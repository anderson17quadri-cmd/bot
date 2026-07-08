"""
sniper_rapido.py  -  Limite diario do "Modo Sniper Rapido" (modo caveira)
============================================================================
So uma responsabilidade: saber quanto o Modo Sniper Rapido ja gastou HOJE
e recusar mais compras quando o limite diario (config.SNIPER_RAPIDO_
LIMITE_DIARIO_USD) for atingido. O limite e OBRIGATORIO, nao "best
effort" - existe para conter o dano maximo possivel num dia mau, dado
que este e o modo de compra mais arriscado do bot.

Ficheiro local: sniper_rapido.json
{
    "data": "2026-07-03",       <- dia (UTC) a que o gasto se refere
    "gasto_usd": 4.0,           <- soma das compras do sniper HOJE
    "compras_hoje": 4            <- numero de compras (so informativo)
}

O contador reinicia sozinho quando o dia (UTC) muda - nao precisas de
nenhum cron nem tarefa agendada, a proxima leitura do dia seguinte ja
comeca do zero.
"""

import json
import os
import threading
from datetime import datetime, timezone

import config

FICHEIRO_ESTADO = "sniper_rapido.json"

# Mesma razao do _lock em posicoes.py/carteira.py: a retentativa do
# Caveira corre agora na sua propria thread (main.py:
# _loop_retentativas_caveira) e tambem chama registar_gasto(). So protege
# o ficheiro contra escrita corrompida por duas threads em simultaneo -
# NAO fecha a janela (pequena e de baixo risco: no maximo 1 compra do
# sniper, tipicamente $1, a mais que o limite diario) entre um pode_gastar()
# e o registar_gasto() correspondente, chamados em dois momentos
# separados por main.py - fechar essa janela exigiria expor o lock a
# main.py, mais complexidade do que o risco justifica aqui.
_lock = threading.Lock()


def _hoje() -> str:
    """Data de hoje em UTC, formato YYYY-MM-DD (a mesma janela usada
    para todos os timestamps do resto do bot)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _carregar() -> dict:
    """Le o estado; se o dia mudou desde a ultima leitura, reinicia o
    contador automaticamente. Ficheiro em falta/corrompido -> estado
    limpo (nunca rebenta)."""
    hoje = _hoje()
    vazio = {"data": hoje, "gasto_usd": 0.0, "compras_hoje": 0}

    if not os.path.exists(FICHEIRO_ESTADO):
        return vazio
    try:
        with open(FICHEIRO_ESTADO, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except (json.JSONDecodeError, OSError):
        return vazio

    if dados.get("data") != hoje:
        return vazio  # novo dia -> reinicia
    return {
        "data": dados.get("data", hoje),
        "gasto_usd": float(dados.get("gasto_usd", 0.0)),
        "compras_hoje": int(dados.get("compras_hoje", 0)),
    }


def _guardar(dados: dict) -> None:
    tmp = FICHEIRO_ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2)
    os.replace(tmp, FICHEIRO_ESTADO)


def gasto_hoje_usd() -> float:
    """Quanto o modo sniper ja gastou hoje (reinicia sozinho por dia)."""
    return _carregar()["gasto_usd"]


def restante_hoje_usd() -> float:
    """Quanto ainda pode gastar hoje antes de bater no limite diario."""
    return max(0.0, config.SNIPER_RAPIDO_LIMITE_DIARIO_USD - gasto_hoje_usd())


def pode_gastar(valor_usd: float) -> bool:
    """True se gastar 'valor_usd' agora NAO ultrapassa o limite diario."""
    return (gasto_hoje_usd() + valor_usd) <= config.SNIPER_RAPIDO_LIMITE_DIARIO_USD


def registar_gasto(valor_usd: float) -> None:
    """Soma 'valor_usd' ao gasto de hoje. Chamar SO depois de uma compra
    do sniper ter mesmo sido efetuada (nao antes, nao em tentativas
    falhadas)."""
    with _lock:
        dados = _carregar()
        dados["gasto_usd"] += valor_usd
        dados["compras_hoje"] += 1
        _guardar(dados)


# --------------------------------------------------------------------------
# Teste rapido:  python3 sniper_rapido.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Gasto hoje       : ${gasto_hoje_usd():.2f}")
    print(f"Limite diario    : ${config.SNIPER_RAPIDO_LIMITE_DIARIO_USD:.2f}")
    print(f"Restante hoje    : ${restante_hoje_usd():.2f}")
