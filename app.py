import os
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import banco
from banco import agora
from companhias import CIDADES, CompanhiaIndisponivel, com_retentativa, obter_companhias

app = Flask(__name__)
app.secret_key = os.environ.get("CHAVE_SECRETA", secrets.token_hex(32))
app.teardown_appcontext(banco.fechar)

NIVEIS = {"atendente": 1, "gerente": 2, "dona": 3}
FORMATO = "%Y-%m-%d %H:%M"
MENSAGEM_INDISPONIVEL = (
    "O sistema da companhia aérea está temporariamente indisponível. "
    "Tentamos novamente automaticamente, mas não obtivemos resposta. "
    "Por favor, tente de novo em alguns minutos."
)


@app.template_filter("moeda")
def moeda(valor):
    texto = f"{valor or 0:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("data_br")
def data_br(valor):
    if not valor:
        return ""
    try:
        if len(valor) > 10:
            return datetime.strptime(valor[:16], FORMATO).strftime("%d/%m/%Y %H:%M")
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return valor


@app.template_filter("cpf")
def formatar_cpf(valor):
    return f"{valor[:3]}.{valor[3:6]}.{valor[6:9]}-{valor[9:]}" if valor and len(valor) == 11 else valor


@app.context_processor
def contexto():
    return {"nivel_admin": NIVEIS.get(session.get("admin_perfil"), 0)}


def cpf_valido(cpf):
    numeros = re.sub(r"\D", "", cpf or "")
    if len(numeros) != 11 or numeros == numeros[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(numeros[i]) * (tamanho + 1 - i) for i in range(tamanho))
        if (soma * 10 % 11) % 10 != int(numeros[tamanho]):
            return False
    return True


def ler_config(chave):
    linha = banco.conectar().execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    return linha["valor"] if linha else None


def salvar_config(chave, valor):
    db = banco.conectar()
    db.execute(
        "INSERT INTO configuracoes (chave, valor) VALUES (?, ?) ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (chave, str(valor)),
    )
    db.commit()


def companhias_ativas():
    return obter_companhias(ler_config("skyhigh_fora_do_ar") == "1")


def usuario_log():
    if session.get("admin_email"):
        return f"{session['admin_perfil']}:{session['admin_email']}"
    return f"cliente:{session.get('cliente_email', 'anonimo')}"


def cliente_logado(funcao):
    @wraps(funcao)
    def interna(*args, **kwargs):
        if "cliente_id" not in session:
            flash("Faça login para continuar.", "aviso")
            return redirect(url_for("login", proximo=request.path))
        return funcao(*args, **kwargs)

    return interna


def admin_requer(nivel):
    def decorador(funcao):
        @wraps(funcao)
        def interna(*args, **kwargs):
            perfil = session.get("admin_perfil")
            if not perfil:
                return redirect(url_for("admin_login"))
            if NIVEIS[perfil] < NIVEIS[nivel]:
                flash("Seu perfil não tem permissão para acessar esta área.", "erro")
                return redirect(url_for("admin_painel"))
            return funcao(*args, **kwargs)

        return interna

    return decorador


def promocao_para(destino):
    return (
        banco.conectar()
        .execute("SELECT * FROM promocoes WHERE destino = ? AND ativa = 1 ORDER BY desconto DESC", (destino,))
        .fetchone()
    )


def calcular_valores(preco_voo, preco_hotel, desconto_pct, comissao_pct):
    subtotal = round(preco_voo + preco_hotel, 2)
    desconto = round(subtotal * desconto_pct / 100, 2)
    comissao = round((subtotal - desconto) * comissao_pct / 100, 2)
    return subtotal, desconto, comissao, round(subtotal - desconto + comissao, 2)


def quartos_disponiveis(hotel_id, checkin, checkout):
    db = banco.conectar()
    total = db.execute("SELECT quartos FROM hoteis WHERE id = ?", (hotel_id,)).fetchone()["quartos"]
    ocupacoes = db.execute(
        "SELECT checkin, checkout, quartos FROM reservas "
        "WHERE hotel_id = ? AND status != 'cancelada' AND checkin < ? AND checkout > ?",
        (hotel_id, checkout, checkin),
    ).fetchall()
    menor = total
    dia = date.fromisoformat(checkin)
    fim = date.fromisoformat(checkout)
    while dia < fim:
        texto = dia.isoformat()
        ocupados = sum(o["quartos"] for o in ocupacoes if o["checkin"] <= texto < o["checkout"])
        menor = min(menor, total - ocupados)
        dia += timedelta(days=1)
    return max(menor, 0)


def verificar_bloqueio(cliente_id):
    db = banco.conectar()
    cliente = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if cliente["bloqueado"]:
        return False
    limite = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    if cliente["desbloqueado_em"] and cliente["desbloqueado_em"] > limite:
        limite = cliente["desbloqueado_em"]
    quantidade = db.execute(
        "SELECT COUNT(*) FROM reservas WHERE cliente_id = ? AND status = 'cancelada' AND cancelado_em >= ?",
        (cliente_id, limite),
    ).fetchone()[0]
    if quantidade >= 3:
        db.execute("UPDATE clientes SET bloqueado = 1 WHERE id = ?", (cliente_id,))
        db.commit()
        banco.registrar_log("sistema", "bloqueio_automatico", f"Cliente {cliente['email']} bloqueado por 3 cancelamentos em 30 dias")
        return True
    return False


def dias_uteis(inicio, quantidade):
    dia = inicio
    while quantidade > 0:
        dia += timedelta(days=1)
        if dia.weekday() < 5:
            quantidade -= 1
    return dia


@app.route("/")
def index():
    promocoes = banco.conectar().execute("SELECT * FROM promocoes WHERE ativa = 1 ORDER BY desconto DESC").fetchall()
    return render_template("index.html", cidades=CIDADES, promocoes=promocoes, hoje=date.today().isoformat())


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "GET":
        return render_template("cadastro.html", dados={})
    nome = " ".join(request.form.get("nome", "").split())
    cpf = re.sub(r"\D", "", request.form.get("cpf", ""))
    email = request.form.get("email", "").strip().lower()
    senha = request.form.get("senha", "")
    db = banco.conectar()
    erros = []
    if len(nome.split()) < 2:
        erros.append("Informe seu nome completo (nome e sobrenome).")
    if not cpf_valido(cpf):
        erros.append("CPF inválido.")
    elif db.execute("SELECT 1 FROM clientes WHERE cpf = ?", (cpf,)).fetchone():
        erros.append("Este CPF já está cadastrado.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        erros.append("E-mail inválido.")
    elif db.execute("SELECT 1 FROM clientes WHERE email = ?", (email,)).fetchone():
        erros.append("Este e-mail já está cadastrado.")
    if len(senha) < 6:
        erros.append("A senha deve ter pelo menos 6 caracteres.")
    if senha != request.form.get("confirmacao", ""):
        erros.append("As senhas não conferem.")
    if erros:
        for erro in erros:
            flash(erro, "erro")
        return render_template("cadastro.html", dados=request.form)
    cursor = db.execute(
        "INSERT INTO clientes (nome, cpf, email, senha_hash, criado_em) VALUES (?, ?, ?, ?, ?)",
        (nome, cpf, email, generate_password_hash(senha), agora()),
    )
    db.commit()
    session.clear()
    session.update(cliente_id=cursor.lastrowid, cliente_nome=nome.split()[0], cliente_email=email)
    banco.registrar_log(usuario_log(), "cadastro", f"Novo cliente {nome}")
    flash("Cadastro realizado com sucesso! Agora é só buscar sua viagem.", "sucesso")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    proximo = request.values.get("proximo", "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        cliente = banco.conectar().execute("SELECT * FROM clientes WHERE email = ?", (email,)).fetchone()
        if cliente and check_password_hash(cliente["senha_hash"], request.form.get("senha", "")):
            session.clear()
            session.update(cliente_id=cliente["id"], cliente_nome=cliente["nome"].split()[0], cliente_email=email)
            if cliente["bloqueado"]:
                flash("Sua conta está bloqueada para novas reservas. Entre em contato com a agência.", "aviso")
            if proximo.startswith("/") and not proximo.startswith("//"):
                return redirect(proximo)
            return redirect(url_for("index"))
        flash("E-mail ou senha incorretos.", "erro")
    return render_template("login.html", admin=False, proximo=proximo)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/voos")
def voos():
    origem = request.args.get("origem", "")
    destino = request.args.get("destino", "")
    data = request.args.get("data", "")
    try:
        passageiros = int(request.args.get("passageiros", 1))
        data_ok = date.fromisoformat(data) >= date.today()
    except ValueError:
        passageiros, data_ok = 0, False
    if origem not in CIDADES or destino not in CIDADES or origem == destino:
        flash("Escolha uma origem e um destino diferentes.", "erro")
        return redirect(url_for("index"))
    if not data_ok or not 1 <= passageiros <= 9:
        flash("Informe uma data a partir de hoje e de 1 a 9 passageiros.", "erro")
        return redirect(url_for("index"))
    resultados = []
    indisponiveis = []
    for companhia in companhias_ativas().values():
        try:
            resultados += com_retentativa(
                companhia.buscar_voos, origem, destino, data, passageiros, tentativas=2, intervalo=0.5
            )
        except CompanhiaIndisponivel:
            indisponiveis.append(companhia.nome)
    agora_dt = datetime.now()
    resultados = [
        voo
        for voo in resultados
        if voo["assentos"] >= passageiros and datetime.strptime(voo["partida"], FORMATO) > agora_dt
    ]
    resultados.sort(key=lambda voo: voo["preco"])
    session["busca"] = {"passageiros": passageiros, "voos": resultados}
    return render_template(
        "voos.html",
        voos=resultados,
        passageiros=passageiros,
        origem=origem,
        destino=destino,
        data=data,
        indisponiveis=indisponiveis,
        mensagem_indisponivel=MENSAGEM_INDISPONIVEL,
        promocao=promocao_para(destino),
    )


@app.route("/reservar/<int:indice>", methods=["GET", "POST"])
@cliente_logado
def reservar(indice):
    busca = session.get("busca")
    if not busca or indice >= len(busca["voos"]):
        flash("Sua busca expirou. Faça uma nova busca de voos.", "aviso")
        return redirect(url_for("index"))
    voo = busca["voos"][indice]
    passageiros = busca["passageiros"]
    db = banco.conectar()
    cliente = db.execute("SELECT * FROM clientes WHERE id = ?", (session["cliente_id"],)).fetchone()
    hoteis = db.execute(
        "SELECT * FROM hoteis WHERE cidade = ? AND ativo = 1 ORDER BY nome", (voo["destino"],)
    ).fetchall()
    promocao = promocao_para(voo["destino"])
    desconto_pct = promocao["desconto"] if promocao else 0
    comissao_pct = float(ler_config("comissao") or 0)
    dia_voo = date.fromisoformat(voo["partida"][:10])
    dados = request.form if request.method == "POST" else {
        "checkin": dia_voo.isoformat(),
        "checkout": (dia_voo + timedelta(days=3)).isoformat(),
        "quartos": "1",
        "forma_pagamento": "cartao",
    }

    def tela():
        return render_template(
            "reservar.html",
            voo=voo,
            indice=indice,
            passageiros=passageiros,
            hoteis=hoteis,
            cliente=cliente,
            promocao=promocao,
            desconto_pct=desconto_pct,
            comissao_pct=comissao_pct,
            dados=dados,
        )

    if request.method == "GET":
        return tela()
    if cliente["bloqueado"]:
        flash("Sua conta está bloqueada para novas reservas. Entre em contato com a agência.", "erro")
        return tela()
    erros = []
    hotel = None
    checkin = checkout = None
    quartos = 0
    preco_hotel = 0
    if request.form.get("hotel_id"):
        hotel = db.execute(
            "SELECT * FROM hoteis WHERE id = ? AND ativo = 1 AND cidade = ?",
            (request.form["hotel_id"], voo["destino"]),
        ).fetchone()
        try:
            inicio = date.fromisoformat(request.form.get("checkin", ""))
            fim = date.fromisoformat(request.form.get("checkout", ""))
            quartos = int(request.form.get("quartos", 1))
        except ValueError:
            inicio = fim = None
        if not hotel:
            erros.append("Hotel inválido.")
        elif not inicio or not fim or fim <= inicio or quartos < 1:
            erros.append("Informe datas de check-in e check-out válidas e ao menos 1 quarto.")
        elif inicio < dia_voo:
            erros.append("O check-in não pode ser antes da data do voo.")
        else:
            checkin, checkout = inicio.isoformat(), fim.isoformat()
            livres = quartos_disponiveis(hotel["id"], checkin, checkout)
            if quartos > livres:
                erros.append(
                    f"O hotel {hotel['nome']} tem apenas {livres} quarto(s) disponível(is) nessas datas."
                )
            preco_hotel = round(hotel["diaria"] * (fim - inicio).days * quartos, 2)
    forma = request.form.get("forma_pagamento")
    if forma == "cartao":
        numero = re.sub(r"\D", "", request.form.get("cartao_numero", ""))
        if not 13 <= len(numero) <= 19:
            erros.append("Número do cartão inválido.")
        if not request.form.get("cartao_nome", "").strip():
            erros.append("Informe o nome impresso no cartão.")
        if not re.fullmatch(r"(0[1-9]|1[0-2])/\d{2}", request.form.get("cartao_validade", "")):
            erros.append("Validade do cartão deve estar no formato MM/AA.")
        if not re.fullmatch(r"\d{3,4}", request.form.get("cartao_cvv", "")):
            erros.append("CVV inválido.")
    elif forma != "boleto":
        erros.append("Escolha a forma de pagamento.")
    if erros:
        for erro in erros:
            flash(erro, "erro")
        return tela()
    subtotal, desconto, comissao, total = calcular_valores(
        voo["preco"] * passageiros, preco_hotel, desconto_pct, comissao_pct
    )
    companhia = companhias_ativas().get(voo["companhia"])
    titular = {"nome": cliente["nome"], "cpf": cliente["cpf"], "email": cliente["email"]}
    try:
        localizador = com_retentativa(companhia.reservar, voo, passageiros, titular)
        bilhete = None
        if forma == "cartao":
            try:
                bilhete = com_retentativa(companhia.emitir_bilhete, localizador)
            except CompanhiaIndisponivel:
                try:
                    companhia.cancelar(localizador)
                except CompanhiaIndisponivel:
                    pass
                raise
    except CompanhiaIndisponivel:
        flash(MENSAGEM_INDISPONIVEL, "erro")
        return tela()
    confirmada = forma == "cartao"
    cursor = db.execute(
        "INSERT INTO reservas (cliente_id, companhia, voo_codigo, origem, destino, data_voo, chegada, passageiros, "
        "preco_voo, hotel_id, checkin, checkout, quartos, preco_hotel, subtotal, desconto, comissao, total, "
        "forma_pagamento, status, pago, localizador, bilhete, criado_em, pago_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cliente["id"], voo["companhia"], voo["codigo"], voo["origem"], voo["destino"], voo["partida"],
            voo["chegada"], passageiros, voo["preco"], hotel["id"] if hotel else None, checkin, checkout,
            quartos, preco_hotel, subtotal, desconto, comissao, total, forma,
            "confirmada" if confirmada else "pendente", 1 if confirmada else 0, localizador, bilhete,
            agora(), agora() if confirmada else None,
        ),
    )
    db.commit()
    reserva_id = cursor.lastrowid
    banco.registrar_log(
        usuario_log(),
        "reserva",
        f"Reserva #{reserva_id} {voo['origem']} -> {voo['destino']} ({'com hotel' if hotel else 'somente aéreo'}), "
        f"total {moeda(total)}, localizador {localizador}",
    )
    if confirmada:
        banco.registrar_log(usuario_log(), "pagamento_cartao", f"Reserva #{reserva_id} paga no cartão, bilhete {bilhete}")
        flash("Pagamento aprovado! Sua reserva está confirmada e o bilhete foi emitido.", "sucesso")
    else:
        flash("Reserva registrada! Pague o boleto para confirmar. A compensação leva até 2 dias úteis.", "sucesso")
    session.pop("busca", None)
    return redirect(url_for("ver_reserva", reserva_id=reserva_id))


def reserva_do_cliente(reserva_id):
    reserva = banco.conectar().execute(
        "SELECT r.*, h.nome AS hotel_nome, h.endereco AS hotel_endereco FROM reservas r "
        "LEFT JOIN hoteis h ON h.id = r.hotel_id WHERE r.id = ? AND r.cliente_id = ?",
        (reserva_id, session["cliente_id"]),
    ).fetchone()
    if not reserva:
        abort(404)
    return reserva


@app.route("/minhas-reservas")
@cliente_logado
def minhas_reservas():
    reservas = banco.conectar().execute(
        "SELECT r.*, h.nome AS hotel_nome FROM reservas r LEFT JOIN hoteis h ON h.id = r.hotel_id "
        "WHERE r.cliente_id = ? ORDER BY r.data_voo DESC",
        (session["cliente_id"],),
    ).fetchall()
    cliente = banco.conectar().execute("SELECT * FROM clientes WHERE id = ?", (session["cliente_id"],)).fetchone()
    return render_template("minhas_reservas.html", reservas=reservas, cliente=cliente)


@app.route("/reserva/<int:reserva_id>")
@cliente_logado
def ver_reserva(reserva_id):
    reserva = reserva_do_cliente(reserva_id)
    boleto = None
    if reserva["status"] == "pendente" and reserva["forma_pagamento"] == "boleto":
        criado = datetime.strptime(reserva["criado_em"][:10], "%Y-%m-%d").date()
        boleto = {
            "linha": f"23793.38128 60{reserva_id:06d}.{reserva['localizador']} 45000.{int(reserva['total'] * 100):010d}",
            "vencimento": dias_uteis(criado, 2).isoformat(),
        }
    return render_template("reserva.html", reserva=reserva, boleto=boleto)


@app.route("/reserva/<int:reserva_id>/cancelar", methods=["GET", "POST"])
@cliente_logado
def cancelar_reserva(reserva_id):
    reserva = reserva_do_cliente(reserva_id)
    if reserva["status"] == "cancelada":
        flash("Esta reserva já está cancelada.", "aviso")
        return redirect(url_for("ver_reserva", reserva_id=reserva_id))
    partida = datetime.strptime(reserva["data_voo"], FORMATO)
    horas = (partida - datetime.now()).total_seconds() / 3600
    if horas <= 0:
        flash("Não é possível cancelar uma reserva cujo voo já partiu.", "erro")
        return redirect(url_for("ver_reserva", reserva_id=reserva_id))
    multa = round(reserva["total"] * 0.20, 2) if reserva["pago"] and horas < 48 else 0
    reembolso = round(reserva["total"] - multa, 2) if reserva["pago"] else 0
    if request.method == "GET":
        return render_template("cancelar.html", reserva=reserva, multa=multa, reembolso=reembolso, horas=horas)
    companhia = companhias_ativas().get(reserva["companhia"])
    try:
        com_retentativa(companhia.cancelar, reserva["localizador"])
    except CompanhiaIndisponivel:
        flash(MENSAGEM_INDISPONIVEL, "erro")
        return redirect(url_for("ver_reserva", reserva_id=reserva_id))
    db = banco.conectar()
    db.execute(
        "UPDATE reservas SET status = 'cancelada', multa = ?, reembolso = ?, cancelado_em = ? WHERE id = ?",
        (multa, reembolso, agora(), reserva_id),
    )
    db.commit()
    banco.registrar_log(
        usuario_log(), "cancelamento", f"Reserva #{reserva_id} cancelada, multa {moeda(multa)}, reembolso {moeda(reembolso)}"
    )
    flash("Reserva cancelada e assentos liberados na companhia aérea.", "sucesso")
    if verificar_bloqueio(session["cliente_id"]):
        flash("Sua conta foi bloqueada para novas reservas por ter 3 cancelamentos em menos de 30 dias.", "aviso")
    return redirect(url_for("ver_reserva", reserva_id=reserva_id))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        funcionario = banco.conectar().execute("SELECT * FROM funcionarios WHERE email = ?", (email,)).fetchone()
        if funcionario and check_password_hash(funcionario["senha_hash"], request.form.get("senha", "")):
            session.clear()
            session.update(
                admin_id=funcionario["id"],
                admin_nome=funcionario["nome"].split()[0],
                admin_email=email,
                admin_perfil=funcionario["perfil"],
            )
            banco.registrar_log(usuario_log(), "login_admin", "Acesso ao painel")
            return redirect(url_for("admin_painel"))
        flash("E-mail ou senha incorretos.", "erro")
    return render_template("login.html", admin=True, proximo="")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_requer("atendente")
def admin_painel():
    db = banco.conectar()
    mes = date.today().strftime("%Y-%m")
    resumo = {
        "pendentes": db.execute("SELECT COUNT(*) FROM reservas WHERE status = 'pendente'").fetchone()[0],
        "confirmadas_mes": db.execute(
            "SELECT COUNT(*) FROM reservas WHERE status = 'confirmada' AND substr(pago_em, 1, 7) = ?", (mes,)
        ).fetchone()[0],
        "clientes": db.execute("SELECT COUNT(*) FROM clientes").fetchone()[0],
        "bloqueados": db.execute("SELECT COUNT(*) FROM clientes WHERE bloqueado = 1").fetchone()[0],
    }
    return render_template("admin/painel.html", resumo=resumo)


@app.route("/admin/reservas")
@admin_requer("atendente")
def admin_reservas():
    status = request.args.get("status", "")
    busca = request.args.get("busca", "").strip()
    sql = (
        "SELECT r.*, c.nome AS cliente_nome, c.cpf AS cliente_cpf, h.nome AS hotel_nome FROM reservas r "
        "JOIN clientes c ON c.id = r.cliente_id LEFT JOIN hoteis h ON h.id = r.hotel_id WHERE 1 = 1"
    )
    parametros = []
    if status:
        sql += " AND r.status = ?"
        parametros.append(status)
    if busca:
        sql += " AND (c.nome LIKE ? OR c.cpf LIKE ? OR r.localizador LIKE ?)"
        termo = f"%{busca}%"
        digitos = re.sub(r"\D", "", busca) or busca
        parametros += [termo, f"%{digitos}%", termo]
    reservas = banco.conectar().execute(sql + " ORDER BY r.criado_em DESC", parametros).fetchall()
    return render_template("admin/reservas.html", reservas=reservas, status=status, busca=busca)


@app.route("/admin/reservas/<int:reserva_id>/confirmar", methods=["POST"])
@admin_requer("atendente")
def admin_confirmar_pagamento(reserva_id):
    db = banco.conectar()
    reserva = db.execute("SELECT * FROM reservas WHERE id = ?", (reserva_id,)).fetchone()
    if not reserva or reserva["status"] != "pendente" or reserva["forma_pagamento"] != "boleto":
        flash("Apenas reservas pendentes pagas por boleto podem ser confirmadas.", "erro")
        return redirect(url_for("admin_reservas"))
    companhia = companhias_ativas().get(reserva["companhia"])
    try:
        bilhete = com_retentativa(companhia.emitir_bilhete, reserva["localizador"])
    except CompanhiaIndisponivel:
        flash(MENSAGEM_INDISPONIVEL, "erro")
        return redirect(url_for("admin_reservas", status="pendente"))
    db.execute(
        "UPDATE reservas SET status = 'confirmada', pago = 1, pago_em = ?, bilhete = ? WHERE id = ?",
        (agora(), bilhete, reserva_id),
    )
    db.commit()
    banco.registrar_log(usuario_log(), "confirmacao_boleto", f"Boleto da reserva #{reserva_id} confirmado, bilhete {bilhete}")
    flash(f"Pagamento da reserva #{reserva_id} confirmado e bilhete {bilhete} emitido.", "sucesso")
    return redirect(url_for("admin_reservas", status="pendente"))


def ler_hotel(formulario):
    try:
        dados = {
            "nome": formulario["nome"].strip(),
            "cidade": formulario["cidade"],
            "endereco": formulario.get("endereco", "").strip(),
            "quartos": int(formulario["quartos"]),
            "diaria": float(formulario["diaria"].replace(",", ".")),
        }
    except (KeyError, ValueError):
        return None
    if not dados["nome"] or dados["cidade"] not in CIDADES or dados["quartos"] < 1 or dados["diaria"] <= 0:
        return None
    return dados


@app.route("/admin/hoteis", methods=["GET", "POST"])
@admin_requer("atendente")
def admin_hoteis():
    db = banco.conectar()
    if request.method == "POST":
        dados = ler_hotel(request.form)
        if not dados:
            flash("Preencha nome, cidade, quantidade de quartos e diária corretamente.", "erro")
        else:
            db.execute(
                "INSERT INTO hoteis (nome, cidade, endereco, quartos, diaria) VALUES (:nome, :cidade, :endereco, :quartos, :diaria)",
                dados,
            )
            db.commit()
            banco.registrar_log(usuario_log(), "cadastro_hotel", f"Hotel {dados['nome']} ({dados['cidade']})")
            flash("Hotel cadastrado com sucesso.", "sucesso")
            return redirect(url_for("admin_hoteis"))
    hoteis = db.execute("SELECT * FROM hoteis ORDER BY cidade, nome").fetchall()
    return render_template("admin/hoteis.html", hoteis=hoteis, cidades=CIDADES)


@app.route("/admin/hoteis/<int:hotel_id>/editar", methods=["GET", "POST"])
@admin_requer("gerente")
def admin_hotel_editar(hotel_id):
    db = banco.conectar()
    hotel = db.execute("SELECT * FROM hoteis WHERE id = ?", (hotel_id,)).fetchone()
    if not hotel:
        abort(404)
    if request.method == "POST":
        dados = ler_hotel(request.form)
        if not dados:
            flash("Preencha os campos corretamente.", "erro")
        else:
            db.execute(
                "UPDATE hoteis SET nome = :nome, cidade = :cidade, endereco = :endereco, quartos = :quartos, "
                "diaria = :diaria WHERE id = :id",
                {**dados, "id": hotel_id},
            )
            db.commit()
            banco.registrar_log(usuario_log(), "edicao_hotel", f"Hotel #{hotel_id} {dados['nome']} atualizado")
            flash("Hotel atualizado.", "sucesso")
            return redirect(url_for("admin_hoteis"))
    return render_template("admin/hotel_editar.html", hotel=hotel, cidades=CIDADES)


@app.route("/admin/hoteis/<int:hotel_id>/alternar", methods=["POST"])
@admin_requer("gerente")
def admin_hotel_alternar(hotel_id):
    db = banco.conectar()
    db.execute("UPDATE hoteis SET ativo = 1 - ativo WHERE id = ?", (hotel_id,))
    db.commit()
    banco.registrar_log(usuario_log(), "status_hotel", f"Hotel #{hotel_id} ativado/desativado")
    return redirect(url_for("admin_hoteis"))


@app.route("/admin/clientes")
@admin_requer("gerente")
def admin_clientes():
    limite = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    clientes = banco.conectar().execute(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM reservas r WHERE r.cliente_id = c.id) AS total_reservas, "
        "(SELECT COUNT(*) FROM reservas r WHERE r.cliente_id = c.id AND r.status = 'cancelada' AND r.cancelado_em >= ?) AS cancelamentos_30 "
        "FROM clientes c ORDER BY c.nome",
        (limite,),
    ).fetchall()
    return render_template("admin/clientes.html", clientes=clientes)


@app.route("/admin/clientes/<int:cliente_id>/<acao>", methods=["POST"])
@admin_requer("gerente")
def admin_cliente_bloqueio(cliente_id, acao):
    if acao not in ("bloquear", "desbloquear"):
        abort(404)
    db = banco.conectar()
    cliente = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if not cliente:
        abort(404)
    if acao == "bloquear":
        db.execute("UPDATE clientes SET bloqueado = 1 WHERE id = ?", (cliente_id,))
    else:
        db.execute("UPDATE clientes SET bloqueado = 0, desbloqueado_em = ? WHERE id = ?", (agora(), cliente_id))
    db.commit()
    banco.registrar_log(usuario_log(), acao, f"Cliente {cliente['email']}")
    flash(f"Cliente {cliente['nome']} {'bloqueado' if acao == 'bloquear' else 'desbloqueado'}.", "sucesso")
    return redirect(url_for("admin_clientes"))


@app.route("/admin/promocoes", methods=["GET", "POST"])
@admin_requer("gerente")
def admin_promocoes():
    db = banco.conectar()
    if request.method == "POST":
        try:
            desconto = float(request.form.get("desconto", "").replace(",", "."))
        except ValueError:
            desconto = 0
        destino = request.form.get("destino")
        descricao = request.form.get("descricao", "").strip()
        if destino not in CIDADES or not descricao or not 0 < desconto < 100:
            flash("Informe destino, descrição e um desconto entre 0 e 100%.", "erro")
        else:
            db.execute(
                "INSERT INTO promocoes (destino, descricao, desconto) VALUES (?, ?, ?)", (destino, descricao, desconto)
            )
            db.commit()
            banco.registrar_log(usuario_log(), "cadastro_promocao", f"{descricao}: {desconto}% para {destino}")
            flash("Promoção criada.", "sucesso")
            return redirect(url_for("admin_promocoes"))
    promocoes = db.execute("SELECT * FROM promocoes ORDER BY ativa DESC, destino").fetchall()
    return render_template("admin/promocoes.html", promocoes=promocoes, cidades=CIDADES)


@app.route("/admin/promocoes/<int:promocao_id>/alternar", methods=["POST"])
@admin_requer("gerente")
def admin_promocao_alternar(promocao_id):
    db = banco.conectar()
    db.execute("UPDATE promocoes SET ativa = 1 - ativa WHERE id = ?", (promocao_id,))
    db.commit()
    banco.registrar_log(usuario_log(), "status_promocao", f"Promoção #{promocao_id} ativada/desativada")
    return redirect(url_for("admin_promocoes"))


@app.route("/admin/relatorios")
@admin_requer("atendente")
def admin_relatorios():
    db = banco.conectar()
    mes = date.today().strftime("%Y-%m")
    basico = db.execute(
        "SELECT "
        "SUM(CASE WHEN status = 'pendente' THEN 1 ELSE 0 END) AS pendentes, "
        "SUM(CASE WHEN status = 'pendente' THEN total ELSE 0 END) AS valor_pendente, "
        "SUM(CASE WHEN status = 'confirmada' AND substr(pago_em, 1, 7) = ? THEN 1 ELSE 0 END) AS vendas_mes, "
        "SUM(CASE WHEN status = 'confirmada' AND substr(pago_em, 1, 7) = ? THEN total ELSE 0 END) AS valor_mes "
        "FROM reservas",
        (mes, mes),
    ).fetchone()
    completo = None
    if NIVEIS[session["admin_perfil"]] >= NIVEIS["gerente"]:
        faturamento = db.execute(
            "SELECT substr(pago_em, 1, 7) AS mes, "
            "COUNT(*) AS vendas, "
            "SUM(CASE WHEN status = 'confirmada' THEN total ELSE multa END) AS faturamento, "
            "SUM(CASE WHEN status = 'confirmada' THEN comissao ELSE 0 END) AS comissao "
            "FROM reservas WHERE pago = 1 GROUP BY mes ORDER BY mes DESC LIMIT 12"
        ).fetchall()
        destinos = db.execute(
            "SELECT destino, COUNT(*) AS quantidade, SUM(total) AS valor FROM reservas "
            "WHERE status = 'confirmada' GROUP BY destino ORDER BY quantidade DESC, valor DESC LIMIT 10"
        ).fetchall()
        pacotes = db.execute(
            "SELECT r.destino, COALESCE(h.nome, 'Somente passagem aérea') AS pacote, COUNT(*) AS quantidade "
            "FROM reservas r LEFT JOIN hoteis h ON h.id = r.hotel_id WHERE r.status = 'confirmada' "
            "GROUP BY r.destino, pacote ORDER BY quantidade DESC LIMIT 10"
        ).fetchall()
        totais = db.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'cancelada' THEN 1 ELSE 0 END) AS canceladas FROM reservas"
        ).fetchone()
        taxa = (totais["canceladas"] or 0) / totais["total"] * 100 if totais["total"] else 0
        completo = {"faturamento": faturamento, "destinos": destinos, "pacotes": pacotes, "totais": totais, "taxa": taxa}
    return render_template("admin/relatorios.html", basico=basico, completo=completo)


@app.route("/admin/configuracoes", methods=["GET", "POST"])
@admin_requer("dona")
def admin_configuracoes():
    if request.method == "POST":
        try:
            comissao = float(request.form.get("comissao", "").replace(",", "."))
        except ValueError:
            comissao = -1
        if not 0 <= comissao <= 50:
            flash("A comissão deve estar entre 0% e 50%.", "erro")
        else:
            falha = "1" if request.form.get("skyhigh_fora_do_ar") else "0"
            salvar_config("comissao", comissao)
            salvar_config("skyhigh_fora_do_ar", falha)
            banco.registrar_log(usuario_log(), "configuracoes", f"Comissão {comissao}%, simular SkyHigh fora do ar: {falha}")
            flash("Configurações salvas.", "sucesso")
            return redirect(url_for("admin_configuracoes"))
    return render_template(
        "admin/configuracoes.html",
        comissao=ler_config("comissao"),
        fora_do_ar=ler_config("skyhigh_fora_do_ar") == "1",
        api_real=bool(os.environ.get("SKYHIGH_URL")),
    )


@app.route("/admin/logs")
@admin_requer("gerente")
def admin_logs():
    logs = banco.conectar().execute("SELECT * FROM logs ORDER BY id DESC LIMIT 300").fetchall()
    return render_template("admin/logs.html", logs=logs)


@app.errorhandler(404)
def nao_encontrado(erro):
    return render_template("erro.html", mensagem="Página não encontrada."), 404


banco.inicializar()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
