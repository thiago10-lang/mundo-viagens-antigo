import os
import sqlite3
from datetime import datetime

from flask import g
from werkzeug.security import generate_password_hash

CAMINHO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mundo_viagens.db")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    cpf TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    bloqueado INTEGER NOT NULL DEFAULT 0,
    desbloqueado_em TEXT,
    criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funcionarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    perfil TEXT NOT NULL CHECK (perfil IN ('atendente', 'gerente', 'dona'))
);

CREATE TABLE IF NOT EXISTS hoteis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    cidade TEXT NOT NULL,
    endereco TEXT,
    quartos INTEGER NOT NULL CHECK (quartos > 0),
    diaria REAL NOT NULL CHECK (diaria > 0),
    ativo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS promocoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    destino TEXT NOT NULL,
    descricao TEXT NOT NULL,
    desconto REAL NOT NULL CHECK (desconto > 0 AND desconto < 100),
    ativa INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS reservas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    companhia TEXT NOT NULL,
    voo_codigo TEXT NOT NULL,
    origem TEXT NOT NULL,
    destino TEXT NOT NULL,
    data_voo TEXT NOT NULL,
    chegada TEXT NOT NULL,
    passageiros INTEGER NOT NULL,
    preco_voo REAL NOT NULL,
    hotel_id INTEGER REFERENCES hoteis(id),
    checkin TEXT,
    checkout TEXT,
    quartos INTEGER NOT NULL DEFAULT 0,
    preco_hotel REAL NOT NULL DEFAULT 0,
    subtotal REAL NOT NULL,
    desconto REAL NOT NULL DEFAULT 0,
    comissao REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL,
    forma_pagamento TEXT NOT NULL CHECK (forma_pagamento IN ('cartao', 'boleto')),
    status TEXT NOT NULL CHECK (status IN ('pendente', 'confirmada', 'cancelada')),
    pago INTEGER NOT NULL DEFAULT 0,
    localizador TEXT NOT NULL,
    bilhete TEXT,
    multa REAL NOT NULL DEFAULT 0,
    reembolso REAL NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL,
    pago_em TEXT,
    cancelado_em TEXT
);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_hora TEXT NOT NULL,
    usuario TEXT NOT NULL,
    acao TEXT NOT NULL,
    detalhe TEXT
);
"""


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conectar():
    if "db" not in g:
        g.db = sqlite3.connect(CAMINHO)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def fechar(erro=None):
    conexao = g.pop("db", None)
    if conexao is not None:
        conexao.close()


def registrar_log(usuario, acao, detalhe=""):
    conexao = conectar()
    conexao.execute(
        "INSERT INTO logs (data_hora, usuario, acao, detalhe) VALUES (?, ?, ?, ?)",
        (agora(), usuario, acao, detalhe),
    )
    conexao.commit()


def popular(conexao):
    funcionarios = [
        ("Marina Souza", "dona@mundoviagens.com", "dona123", "dona"),
        ("Carlos Lima", "gerente@mundoviagens.com", "gerente123", "gerente"),
        ("Ana Pereira", "atendente@mundoviagens.com", "atendente123", "atendente"),
    ]
    conexao.executemany(
        "INSERT INTO funcionarios (nome, email, senha_hash, perfil) VALUES (?, ?, ?, ?)",
        [(nome, email, generate_password_hash(senha), perfil) for nome, email, senha, perfil in funcionarios],
    )
    conexao.executemany(
        "INSERT INTO hoteis (nome, cidade, endereco, quartos, diaria) VALUES (?, ?, ?, ?, ?)",
        [
            ("Copacabana Mar Hotel", "Rio de Janeiro", "Av. Atlântica, 1500", 5, 420.00),
            ("Rio Centro Inn", "Rio de Janeiro", "Rua da Carioca, 80", 8, 260.00),
            ("Pelourinho Palace", "Salvador", "Largo do Pelourinho, 12", 4, 310.00),
            ("Boa Viagem Resort", "Recife", "Av. Boa Viagem, 3000", 6, 350.00),
            ("Iracema Praia Hotel", "Fortaleza", "Av. Beira Mar, 2200", 6, 290.00),
            ("Floripa Beach Hotel", "Florianópolis", "Av. Beira-Mar Norte, 900", 5, 380.00),
            ("Alfama Boutique", "Lisboa", "Rua de São Miguel, 25", 3, 780.00),
            ("Le Petit Montmartre", "Paris", "Rue Lepic, 40", 3, 1150.00),
            ("Orlando Parks Suites", "Orlando", "International Dr, 7000", 10, 690.00),
            ("Palermo Soho Hotel", "Buenos Aires", "Honduras, 4800", 5, 450.00),
        ],
    )
    conexao.executemany(
        "INSERT INTO promocoes (destino, descricao, desconto) VALUES (?, ?, ?)",
        [
            ("Salvador", "Primavera na Bahia", 15),
            ("Lisboa", "Europa em alta", 10),
        ],
    )
    conexao.executemany(
        "INSERT INTO configuracoes (chave, valor) VALUES (?, ?)",
        [("comissao", "10"), ("skyhigh_fora_do_ar", "0")],
    )
    conexao.execute(
        "INSERT INTO clientes (nome, cpf, email, senha_hash, criado_em) VALUES (?, ?, ?, ?, ?)",
        ("João da Silva", "52998224725", "cliente@teste.com", generate_password_hash("cliente123"), agora()),
    )


def inicializar():
    conexao = sqlite3.connect(CAMINHO)
    conexao.executescript(ESQUEMA)
    if conexao.execute("SELECT COUNT(*) FROM funcionarios").fetchone()[0] == 0:
        popular(conexao)
    conexao.commit()
    conexao.close()
