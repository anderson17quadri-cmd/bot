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
    decimais: int | None = None,
    dex: str | None = None,
    modo: str = "normal",
    pool_address: str | None = None,
) -> dict:
    # 'decimais' e opcional (posicoes antigas nao o tem): serve so para o
    # dashboard poder converter o preco por UNIDADE MINIMA (raw, que e
    # como preco_compra_usd/quantidade_tokens sao guardados internamente)
    # no preco por TOKEN INTEIRO que um humano reconhece (ex: BONK tem 5
    # decimais -> preco_por_token = preco_raw * 10^5). A matematica do
    # stop-loss NAO usa isto - trabalha sempre em raw, por racios.
    # 'dex' e 'modo' (normal|bonding_curve|sniper_rapido|copy_trading) sao
    # guardados aqui para, ao VENDER, sabermos que valores repassar ao
    # historico (carteira.registar_venda) - as estatisticas por modo/DEX
    # do dashboard precisam disto tanto na compra como na venda.
    posicoes = carregar_posicoes()
    posicao = {
        "mint": mint,
        "simbolo": simbolo,
        "valor_investido_usd": valor_investido_usd,
        "preco_compra_usd": preco_compra_usd,
        "quantidade_tokens": quantidade_tokens,
        "decimais": decimais,
        "dex": dex,
        "modo": modo,
        # Conta do pool/bonding curve (se soubermos) - usada pela deteccao
        # de reversao (momentum.py) para EXCLUIR o pool das contagens de
        # compra/venda. Sem isto, toda compra soma 1 "venda" do lado do
        # pool (o pool perde saldo quando alguem compra) e vice-versa -
        # o racio ficaria sempre ~1:1 por construcao, inutil como sinal.
        "pool_address": pool_address,
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
