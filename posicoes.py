"""
posicoes.py  -  Gestao de posicoes abertas
============================================
Guarda o estado do que o bot "comprou" (real ou simulado) num ficheiro JSON.
"""

import json
import os
from datetime import datetime, timezone

import config


def carregar_posicoes() -> dict:
    caminho = config.FICHEIRO_POSICOES
    if not os.path.exists(caminho):
        return {}
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def guardar_posicoes(posicoes: dict) -> None:
    caminho = config.FICHEIRO_POSICOES
    caminho_tmp = caminho + ".tmp"
    with open(caminho_tmp, "w", encoding="utf-8") as f:
        json.dump(posicoes, f, indent=2, ensure_ascii=False)
    os.replace(caminho_tmp, caminho)


def abrir_posicao(
    mint: str,
    simbolo: str,
    valor_investido_usd: float,
    preco_compra_usd: float,
    quantidade_tokens: float,
    dry_run: bool,
) -> dict:
    posicoes = carregar_posicoes()
    posicao = {
        "mint": mint,
        "simbolo": simbolo,
        "valor_investido_usd": valor_investido_usd,
        "preco_compra_usd": preco_compra_usd,
        "quantidade_tokens": quantidade_tokens,
        "pico_preco_usd": preco_compra_usd,
        "take_profit_disparado": False,
        "timestamp_compra": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
    }
    posicoes[mint] = posicao
    guardar_posicoes(posicoes)
    return posicao


def fechar_posicao(mint: str) -> None:
    posicoes = carregar_posicoes()
    if mint in posicoes:
        del posicoes[mint]
        guardar_posicoes(posicoes)


def atualizar_posicao(mint: str, **campos) -> None:
    posicoes = carregar_posicoes()
    if mint not in posicoes:
        return
    posicoes[mint].update(campos)
    guardar_posicoes(posicoes)


def atualizar_pico(mint: str, preco_atual_usd: float) -> None:
    posicoes = carregar_posicoes()
    if mint not in posicoes:
        return
    if preco_atual_usd > posicoes[mint].get("pico_preco_usd", 0):
        posicoes[mint]["pico_preco_usd"] = preco_atual_usd
        guardar_posicoes(posicoes)


def listar_posicoes_abertas() -> dict:
    return carregar_posicoes()


if __name__ == "__main__":
    print("Posicoes abertas atualmente:")
    for mint, p in listar_posicoes_abertas().items():
        print(f"  {p['simbolo']} ({mint[:8]}...) - "
              f"${p['valor_investido_usd']} investidos, "
              f"dry_run={p['dry_run']}")
    if not listar_posicoes_abertas():
        print("  (nenhuma)")
