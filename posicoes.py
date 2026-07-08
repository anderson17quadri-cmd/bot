"""
posicoes.py  -  Gestao de posicoes abertas
============================================
Guarda o estado do que o bot "comprou" (real ou simulado) num ficheiro JSON.
"""

import json
import os
import threading
from datetime import datetime, timezone

import config

# Protege o ciclo carregar->modificar->guardar contra dois threads a
# escreverem ao mesmo tempo (desde que a retentativa do Caveira passou a
# correr na sua propria thread - ver main.py:_loop_retentativas_caveira -
# ha pela primeira vez mais que uma thread capaz de comprar/vender e
# tocar neste ficheiro). Sem isto, dois "carregar, alterar, guardar"
# concorrentes podiam perder a escrita um do outro (o ultimo a guardar
# "ganha", apagando a alteracao do outro sem erro nenhum - corrupcao
# silenciosa do estado das posicoes).
_lock = threading.Lock()


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
    with _lock:
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


def fechar_posicao(mint: str, saida_usd: float | None = None,
                   motivo_saida: str | None = None) -> None:
    """Apaga a posicao e, antes disso, regista o trade fechado na memoria
    semanal (memoria/trades_fechados.jsonl). 'saida_usd' e o valor da
    venda FINAL (as vendas parciais anteriores ja estao acumuladas na
    propria posicao em 'valor_realizado_parcial_usd'). Os dois campos
    novos sao opcionais - chamadas antigas continuam a funcionar."""
    with _lock:
        posicoes = carregar_posicoes()
        if mint in posicoes:
            _registar_trade_fechado(posicoes[mint], saida_usd, motivo_saida)
            del posicoes[mint]
            guardar_posicoes(posicoes)


def _registar_trade_fechado(pos: dict, saida_usd, motivo_saida) -> None:
    """Regista o fecho na memoria semanal. NUNCA levanta - o registo e
    informativo e nao pode, em caso algum, impedir o fecho da posicao."""
    try:
        import memoria
        ts_abertura = pos.get("timestamp_compra")
        agora = datetime.now(timezone.utc)
        duracao_min = None
        if ts_abertura:
            try:
                aberta = datetime.fromisoformat(ts_abertura)
                duracao_min = round((agora - aberta).total_seconds() / 60, 1)
            except (TypeError, ValueError):
                pass
        entrada = pos.get("valor_investido_usd") or 0.0
        # saida TOTAL = parciais ja realizadas (take-profit) + venda final
        saida_total = None
        pl = None
        if saida_usd is not None:
            saida_total = pos.get("valor_realizado_parcial_usd", 0.0) + saida_usd
            pl = saida_total - entrada
        memoria.registar_trade_fechado({
            "timestamp_abertura": ts_abertura,
            "timestamp_fecho": agora.isoformat(),
            "token": pos.get("simbolo"),
            "mint": pos.get("mint"),
            "chain": pos.get("chain", "solana"),
            "modo": pos.get("modo", "normal"),
            "entrada_usd": round(entrada, 4),
            "saida_usd": round(saida_total, 4) if saida_total is not None else None,
            "pl_usd": round(pl, 4) if pl is not None else None,
            "pl_pct": round(pl / entrada * 100, 2) if (pl is not None and entrada) else None,
            "duracao_min": duracao_min,
            "motivo_saida": motivo_saida or "desconhecido",
        })
    except Exception as e:
        print(f"[memoria] falha ao registar trade fechado: {e}")


def atualizar_posicao(mint: str, **campos) -> None:
    with _lock:
        posicoes = carregar_posicoes()
        if mint not in posicoes:
            return
        posicoes[mint].update(campos)
        guardar_posicoes(posicoes)


def atualizar_pico(mint: str, preco_atual_usd: float) -> None:
    with _lock:
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
