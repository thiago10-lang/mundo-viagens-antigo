import json
import os
import random
import string
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

CIDADES = [
    "São Paulo",
    "Rio de Janeiro",
    "Brasília",
    "Salvador",
    "Recife",
    "Fortaleza",
    "Florianópolis",
    "Porto Alegre",
    "Buenos Aires",
    "Santiago",
    "Lisboa",
    "Paris",
    "Orlando",
]

INTERNACIONAIS = {"Buenos Aires", "Santiago", "Lisboa", "Paris", "Orlando"}


class CompanhiaIndisponivel(Exception):
    pass


class CompanhiaAerea:
    nome = ""

    def buscar_voos(self, origem, destino, data, passageiros):
        raise NotImplementedError

    def reservar(self, voo, passageiros, passageiro):
        raise NotImplementedError

    def emitir_bilhete(self, localizador):
        raise NotImplementedError

    def cancelar(self, localizador):
        raise NotImplementedError


class SkyHighAPI(CompanhiaAerea):
    nome = "SkyHigh"

    def __init__(self, url_base, chave, timeout=4):
        if not url_base.lower().startswith("https://"):
            raise ValueError("A API da SkyHigh deve ser acessada via HTTPS.")
        self.url_base = url_base.rstrip("/")
        self.chave = chave
        self.timeout = timeout

    def _chamar(self, metodo, caminho, dados=None):
        corpo = json.dumps(dados).encode("utf-8") if dados is not None else None
        requisicao = urllib.request.Request(
            self.url_base + caminho,
            data=corpo,
            method=metodo,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.chave},
        )
        try:
            with urllib.request.urlopen(requisicao, timeout=self.timeout) as resposta:
                conteudo = resposta.read().decode("utf-8")
                return json.loads(conteudo) if conteudo else {}
        except Exception as erro:
            raise CompanhiaIndisponivel(str(erro)) from erro

    def buscar_voos(self, origem, destino, data, passageiros):
        parametros = urllib.parse.urlencode(
            {"origem": origem, "destino": destino, "data": data, "passageiros": passageiros}
        )
        voos = self._chamar("GET", "/voos?" + parametros).get("voos", [])
        for voo in voos:
            voo["companhia"] = self.nome
        return voos

    def reservar(self, voo, passageiros, passageiro):
        dados = {"voo": voo["codigo"], "partida": voo["partida"], "passageiros": passageiros, "titular": passageiro}
        return self._chamar("POST", "/reservas", dados)["localizador"]

    def emitir_bilhete(self, localizador):
        return self._chamar("POST", f"/reservas/{localizador}/bilhete", {})["bilhete"]

    def cancelar(self, localizador):
        self._chamar("DELETE", f"/reservas/{localizador}")


class SkyHighSimulada(CompanhiaAerea):
    nome = "SkyHigh"

    def __init__(self, fora_do_ar=False):
        self.fora_do_ar = fora_do_ar

    def _verificar(self):
        if self.fora_do_ar:
            raise CompanhiaIndisponivel("SkyHigh fora do ar")

    def buscar_voos(self, origem, destino, data, passageiros):
        self._verificar()
        gerador = random.Random(f"{origem}|{destino}|{data}")
        dia = datetime.strptime(data, "%Y-%m-%d")
        internacional = origem in INTERNACIONAIS or destino in INTERNACIONAIS
        voos = []
        for _ in range(gerador.randint(3, 6)):
            partida = dia + timedelta(hours=gerador.randint(5, 22), minutes=gerador.choice([0, 15, 30, 45]))
            duracao = gerador.randint(420, 720) if internacional else gerador.randint(60, 210)
            preco = gerador.uniform(1900, 5200) if internacional else gerador.uniform(250, 1400)
            voos.append(
                {
                    "companhia": self.nome,
                    "codigo": f"SH{gerador.randint(1000, 9999)}",
                    "origem": origem,
                    "destino": destino,
                    "partida": partida.strftime("%Y-%m-%d %H:%M"),
                    "chegada": (partida + timedelta(minutes=duracao)).strftime("%Y-%m-%d %H:%M"),
                    "preco": round(preco, 2),
                    "assentos": gerador.randint(0, 40),
                }
            )
        return sorted(voos, key=lambda voo: voo["partida"])

    def reservar(self, voo, passageiros, passageiro):
        self._verificar()
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

    def emitir_bilhete(self, localizador):
        self._verificar()
        return "957-" + "".join(random.choices(string.digits, k=10))

    def cancelar(self, localizador):
        self._verificar()


def com_retentativa(funcao, *argumentos, tentativas=3, intervalo=1.0):
    for tentativa in range(1, tentativas + 1):
        try:
            return funcao(*argumentos)
        except CompanhiaIndisponivel:
            if tentativa == tentativas:
                raise
            time.sleep(intervalo)


def obter_companhias(simular_falha=False):
    url = os.environ.get("SKYHIGH_URL")
    if url:
        skyhigh = SkyHighAPI(url, os.environ.get("SKYHIGH_CHAVE", ""))
    else:
        skyhigh = SkyHighSimulada(simular_falha)
    return {companhia.nome: companhia for companhia in [skyhigh]}
