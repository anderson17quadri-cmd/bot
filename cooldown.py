"""
cooldown.py  -  Evita re-analisar o MESMO token varias vezes seguidas
========================================================================
O detector.py ja evita repetir o mesmo POOL (pool_address), mas um
token pode aparecer com poolS diferentes em pouco tempo - o caso mais
comum e um token do pump.fun: a bonding curve tem um pool_address, e
quando migra para a PumpSwap/Raydium a GeckoTerminal lista isso como
um pool NOVO, com outro endereco, mesmo sendo o mesmo token_mint. O
dedup por pool_address deixa passar os dois como "novos", gastando uma
chamada a DeepSeek e poluindo o radar com o mesmo token repetido.

Este modulo guarda, por MINT (nao por pool), quando foi a ultima vez
que o token passou pela analise completa. Persistido em ficheiro (nao
so em memoria) de proposito: o DetectorPools._vistos e um set() em
memoria que esvazia sempre que o bot reinicia (ex: pelo botao Iniciar/
Parar Bot do dashboard) - sem persistir isto aqui, cada reinicio voltava
a reanalisar os ultimos tokens vistos na sessao anterior.

Ficheiro local: vistos_recentemente.json
{
    "<mint>": "2026-07-03T15:00:00+00:00",   <- ultima analise completa
    ...
}

IMPORTANTE: isto NAO se aplica a posicoes ja abertas - o preco dessas
e verificado no seu proprio ciclo (main.verificar_posicoes), que nunca
passa por aqui.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import config

FICHEIRO_COOLDOWN = "vistos_recentemente.json"


def _carregar() -> dict:
    """Le o ficheiro. Em falta/corrompido -> dicionario vazio (nunca
    rebenta - pior caso e reanalisar um token que nao devia)."""
    if not os.path.exists(FICHEIRO_COOLDOWN):
        return {}
    try:
        with open(FICHEIRO_COOLDOWN, "r", encoding="utf-8") as f:
            dados = json.load(f)
            return dados if isinstance(dados, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _guardar(dados: dict) -> None:
    """Escrita atomica (tmp + rename), como o resto do bot."""
    tmp = FICHEIRO_COOLDOWN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2)
    os.replace(tmp, FICHEIRO_COOLDOWN)


def _purgar_antigos(dados: dict) -> dict:
    """Remove entradas mais antigas que o cooldown - o ficheiro nunca
    cresce sem limite, so guarda o que ainda e relevante."""
    limite = datetime.now(timezone.utc) - timedelta(minutes=config.COOLDOWN_REANALISE_MINUTOS)
    resultado = {}
    for mint, timestamp_iso in dados.items():
        try:
            quando = datetime.fromisoformat(timestamp_iso)
        except (TypeError, ValueError):
            continue  # entrada corrompida - descarta-a de qualquer forma
        if quando >= limite:
            resultado[mint] = timestamp_iso
    return resultado


def foi_analisado_recentemente(mint: str) -> bool:
    """True se este mint ja passou pela analise completa dentro da
    janela de cooldown (config.COOLDOWN_REANALISE_MINUTOS)."""
    dados = _carregar()
    timestamp_iso = dados.get(mint)
    if not timestamp_iso:
        return False
    try:
        quando = datetime.fromisoformat(timestamp_iso)
    except (TypeError, ValueError):
        return False
    limite = datetime.now(timezone.utc) - timedelta(minutes=config.COOLDOWN_REANALISE_MINUTOS)
    return quando >= limite


def registar_analise(mint: str) -> None:
    """Marca este mint como "acabado de analisar agora". Aproveita para
    purgar entradas antigas, para o ficheiro nao crescer para sempre."""
    dados = _purgar_antigos(_carregar())
    dados[mint] = datetime.now(timezone.utc).isoformat()
    _guardar(dados)


# --------------------------------------------------------------------------
# Teste rapido:  python3 cooldown.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    dados = _carregar()
    print(f"Cooldown: {len(dados)} mint(s) em janela de {config.COOLDOWN_REANALISE_MINUTOS} min")
    for mint, quando in dados.items():
        print(f"  {mint[:12]}...  ->  {quando}")
