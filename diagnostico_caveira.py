"""
diagnostico_caveira.py  -  Analisa o desempenho REAL do Modo Caveira
=======================================================================
Le o teu carteira.json e cruza compras com vendas do modo "sniper_rapido"
(uma compra e a venda mais proxima no tempo, para o mesmo mint, sao o
mesmo "trade"). Mostra:

  - Win rate, lucro/prejuizo total, media por trade
  - Distribuicao de liquidez/idade/holders NAS PERDAS vs NOS GANHOS
    (so disponivel para compras feitas DEPOIS desta funcionalidade -
    compras antigas nao tinham este contexto guardado, aparecem como
    "sem dados")
  - Distribuicao por DEX (ja disponivel desde a Sessao dos Blocos 1+5)

Uso:  python3 diagnostico_caveira.py [caminho_para_carteira.json]
(por defeito le ./carteira.json)
"""

import json
import sys
from collections import defaultdict


def carregar(caminho: str) -> list:
    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f).get("historico", [])


def cruzar_trades(historico: list, modo: str) -> list:
    """Junta cada VENDA do modo dado com a COMPRA mais recente do mesmo
    mint que a precede - forma um "trade" completo (compra+venda).
    Ignora compras sem venda correspondente (posicao ainda aberta)."""
    compras_por_mint: dict = defaultdict(list)
    for h in sorted(historico, key=lambda x: x.get("timestamp", "")):
        if h.get("tipo") == "compra" and h.get("modo") == modo:
            compras_por_mint[h.get("mint")].append(h)

    trades = []
    for h in sorted(historico, key=lambda x: x.get("timestamp", "")):
        if h.get("tipo") != "venda" or h.get("modo") != modo:
            continue
        mint = h.get("mint")
        candidatas = compras_por_mint.get(mint) or []
        if not candidatas:
            continue
        compra = candidatas.pop(0)  # a mais antiga por vender ainda disponivel
        trades.append({"compra": compra, "venda": h, "lucro_usd": h.get("lucro_usd", 0.0)})
    return trades


def _bucket_liquidez(liq):
    if liq is None:
        return "sem dados"
    if liq < 1000:
        return "<$1000"
    if liq < 2000:
        return "$1000-2000"
    if liq < 5000:
        return "$2000-5000"
    return ">=$5000"


def _resumo_grupo(trades: list) -> str:
    if not trades:
        return "  (nenhum trade)"
    n = len(trades)
    ganhos = [t for t in trades if t["lucro_usd"] > 0]
    lucro_total = sum(t["lucro_usd"] for t in trades)
    return (f"  n={n:3d}  win_rate={len(ganhos)/n*100:5.1f}%  "
            f"lucro_total=${lucro_total:+.2f}  media=${lucro_total/n:+.3f}")


def main(caminho: str) -> None:
    historico = carregar(caminho)
    trades = cruzar_trades(historico, "sniper_rapido")

    print(f"=== MODO CAVEIRA (sniper_rapido): {len(trades)} trade(s) fechado(s) ===\n")
    if not trades:
        print("Sem trades fechados deste modo em", caminho)
        return

    ganhos = [t for t in trades if t["lucro_usd"] > 0]
    perdas = [t for t in trades if t["lucro_usd"] <= 0]
    lucro_total = sum(t["lucro_usd"] for t in trades)
    print(f"Win rate         : {len(ganhos)}/{len(trades)} = {len(ganhos)/len(trades)*100:.1f}%")
    print(f"Lucro/prejuizo   : ${lucro_total:+.2f}")
    print(f"Media por trade  : ${lucro_total/len(trades):+.3f}")
    print(f"Media das perdas : ${sum(t['lucro_usd'] for t in perdas)/len(perdas):+.3f}" if perdas else "")
    print(f"Media dos ganhos : ${sum(t['lucro_usd'] for t in ganhos)/len(ganhos):+.3f}" if ganhos else "")

    print("\n=== POR FAIXA DE LIQUIDEZ (na compra) ===")
    por_liq = defaultdict(list)
    for t in trades:
        por_liq[_bucket_liquidez(t["compra"].get("liquidez_usd"))].append(t)
    for faixa in ["sem dados", "<$1000", "$1000-2000", "$2000-5000", ">=$5000"]:
        if faixa in por_liq:
            print(f"{faixa:>14}:{_resumo_grupo(por_liq[faixa])}")

    print("\n=== POR DEX ===")
    por_dex = defaultdict(list)
    for t in trades:
        por_dex[t["compra"].get("dex") or "desconhecido"].append(t)
    for dex, ts in sorted(por_dex.items(), key=lambda kv: -len(kv[1])):
        print(f"{dex:>14}:{_resumo_grupo(ts)}")

    print("\n=== POR CONCENTRACAO DO MAIOR HOLDER (na compra) ===")
    por_holder = defaultdict(list)
    for t in trades:
        pct = t["compra"].get("top_holder_pct")
        if pct is None:
            chave = "sem dados"
        elif pct < 15:
            chave = "<15%"
        elif pct < 30:
            chave = "15-30%"
        else:
            chave = ">=30%"
        por_holder[chave].append(t)
    for chave in ["sem dados", "<15%", "15-30%", ">=30%"]:
        if chave in por_holder:
            print(f"{chave:>14}:{_resumo_grupo(por_holder[chave])}")

    sem_contexto = sum(1 for t in trades if t["compra"].get("liquidez_usd") is None)
    if sem_contexto:
        print(f"\nNOTA: {sem_contexto}/{len(trades)} trade(s) sem 'liquidez_usd' guardado "
              f"(compras anteriores a esta funcionalidade) - a analise por faixa fica "
              f"mais fiavel a medida que mais trades novos se acumularem.")


if __name__ == "__main__":
    caminho = sys.argv[1] if len(sys.argv) > 1 else "carteira.json"
    main(caminho)
