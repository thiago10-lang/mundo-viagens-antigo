# Mundo Viagens – Sistema Web de Pacotes de Viagem

## Como executar
1. Instale o Python 3.10 ou superior.
2. Dê dois cliques em `iniciar.bat` (ou rode `pip install -r requirements.txt` e depois `python app.py`).
3. Acesse http://127.0.0.1:5000

## Usuários de teste
| Perfil | E-mail | Senha |
|---|---|---|
| Cliente | cliente@teste.com | cliente123 |
| Atendente | atendente@mundoviagens.com | atendente123 |
| Gerente | gerente@mundoviagens.com | gerente123 |
| Dona | dona@mundoviagens.com | dona123 |

Painel da agência: http://127.0.0.1:5000/admin/login

## Estrutura
- `app.py` – rotas e regras de negócio (RN1 a RN6)
- `banco.py` – banco SQLite, tabelas, dados iniciais e log de auditoria
- `companhias.py` – integração com companhias aéreas (interface `CompanhiaAerea`, cliente HTTPS `SkyHighAPI`, simulador `SkyHighSimulada` e retentativa automática)
- `templates/` e `static/` – telas responsivas

## API real da SkyHigh
Enquanto a API não é liberada, o sistema usa um simulador. Quando estiver disponível, basta definir as variáveis de ambiente `SKYHIGH_URL` (obrigatoriamente `https://`) e `SKYHIGH_CHAVE`. Novas companhias podem ser adicionadas criando outra classe que herde de `CompanhiaAerea` e registrando-a em `obter_companhias()`.
