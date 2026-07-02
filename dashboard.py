"""
dashboard.py  -  Dashboard web local do bot (so LEITURA)
==========================================================
Um pequeno servidor Flask que le os ficheiros JSON gerados pelo bot
(carteira.json e posicoes.json) e mostra tudo numa pagina web com
graficos, acessivel no browser do telemovel.

IMPORTANTE:
- Este ficheiro NUNCA escreve nos JSON. Quem gere esses dados e o
  main.py. Aqui e so visualizacao.
- Nao importa o executor.py (que depende de 'solders', uma biblioteca
  nativa em Rust). A cotacao Jupiter e refeita aqui so com 'requests',
  que e pure-Python e ja esta instalada.

Como correr:
    python3 dashboard.py
e depois abrir http://localhost:5000 no browser.
"""

import json
import os
import time

import requests
from flask import Flask, jsonify, render_template

import config  # so para ler DRY_RUN, SALDO_VIRTUAL_INICIAL, etc.

app = Flask(__name__)

# Mesmos endpoints do executor.py - so o /quote, que e uma consulta
# inofensiva (nunca compra nem vende nada)
JUPITER_QUOTE_URL = "https://public.jupiterapi.com/quote"

# Cache de cotacoes: {mint: (timestamp_da_consulta, valor_usd)}
# Evita bombardear a API da Jupiter - cada mint e consultado no maximo
# uma vez a cada CACHE_SEGUNDOS, mesmo que a pagina faca polling rapido.
_cache_cotacoes: dict = {}
CACHE_SEGUNDOS = 30


# ==========================================================================
# Leitura segura dos ficheiros JSON
# ==========================================================================
def _ler_json(caminho: str, defeito):
    """Le um ficheiro JSON. Se nao existir (bot nunca correu) ou estiver
    corrompido, devolve o valor por defeito em vez de rebentar."""
    if not os.path.exists(caminho):
        return defeito
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return defeito


def _ler_carteira() -> dict:
    """carteira.json - saldo virtual + historico de compras/vendas."""
    return _ler_json("carteira.json", {
        "saldo_inicial_usd": config.SALDO_VIRTUAL_INICIAL,
        "saldo_atual_usd": config.SALDO_VIRTUAL_INICIAL,
        "historico": [],
    })


def _ler_posicoes() -> dict:
    """posicoes.json - posicoes abertas neste momento ({mint: {...}})."""
    return _ler_json(config.FICHEIRO_POSICOES, {})


# ==========================================================================
# Cotacao Jupiter (preco atual estimado das posicoes abertas)
# ==========================================================================
def _valor_atual_usd(mint: str, quantidade_tokens: float):
    """Pergunta a Jupiter quanto valem 'quantidade_tokens' deste mint em
    USDC (1 USDC = 1 USD). E a mesma logica do executor.vender_token em
    dry-run. Devolve None se a consulta falhar (ex: sem internet, token
    sem liquidez) - o frontend mostra "N/A" nesse caso."""
    agora = time.time()

    # 1) Ja temos um valor recente em cache? Usa esse.
    em_cache = _cache_cotacoes.get(mint)
    if em_cache and (agora - em_cache[0]) < CACHE_SEGUNDOS:
        return em_cache[1]

    # 2) Senao, pergunta a Jupiter (consulta apenas - nao mexe em dinheiro)
    try:
        resposta = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": mint,
            "outputMint": config.MINT_USDC,
            "amount": int(quantidade_tokens),
            "slippageBps": config.SLIPPAGE_BPS,
        }, timeout=8)
        resposta.raise_for_status()
        # outAmount vem em unidades minimas de USDC (6 casas decimais)
        valor = float(resposta.json()["outAmount"]) / 1_000_000
    except Exception:
        valor = None  # qualquer falha -> "N/A" no dashboard

    _cache_cotacoes[mint] = (agora, valor)
    return valor


# ==========================================================================
# Rotas
# ==========================================================================
@app.route("/")
def pagina_principal():
    """Serve a pagina HTML do dashboard (templates/index.html)."""
    return render_template("index.html")


@app.route("/api/resumo")
def api_resumo():
    """Devolve tudo o que os cartoes, graficos e tabela de historico
    precisam, calculado a partir do carteira.json."""
    carteira = _ler_carteira()
    posicoes = _ler_posicoes()

    inicial = carteira.get("saldo_inicial_usd", 0.0)
    saldo_livre = carteira.get("saldo_atual_usd", 0.0)
    historico = carteira.get("historico", [])

    # --- Contadores e win rate (so vendas fechadas contam) -------------
    compras = [h for h in historico if h.get("tipo") == "compra"]
    vendas = [h for h in historico if h.get("tipo") == "venda"]
    vendas_com_lucro = [v for v in vendas if v.get("lucro_usd", 0) > 0]
    win_rate = (len(vendas_com_lucro) / len(vendas) * 100) if vendas else None

    # --- Valor investido nas posicoes ainda abertas --------------------
    valor_posicoes = sum(
        p.get("valor_investido_usd", 0.0) for p in posicoes.values()
    )

    # --- Lucro/prejuizo total (saldo livre + posicoes vs. inicial) -----
    valor_total = saldo_livre + valor_posicoes
    lucro_total = valor_total - inicial
    lucro_total_pct = (lucro_total / inicial * 100) if inicial else 0.0

    # --- Curva de saldo: recalcula o saldo apos cada operacao ----------
    # Comeca no saldo inicial; cada compra tira dinheiro, cada venda poe.
    ordenado = sorted(historico, key=lambda h: h.get("timestamp", ""))
    saldo = inicial
    curva = [{"timestamp": None, "saldo": round(saldo, 2)}]  # ponto de partida
    for h in ordenado:
        if h.get("tipo") == "compra":
            saldo -= h.get("valor_usd", 0.0)
        elif h.get("tipo") == "venda":
            saldo += h.get("valor_usd", 0.0)
        curva.append({
            "timestamp": h.get("timestamp"),
            "saldo": round(saldo, 2),
        })

    # --- Ganhos vs perdas (para o grafico de "velas" de P/L) -----------
    total_ganho = sum(v.get("lucro_usd", 0) for v in vendas if v.get("lucro_usd", 0) > 0)
    total_perdido = sum(-v.get("lucro_usd", 0) for v in vendas if v.get("lucro_usd", 0) < 0)
    velas = [  # uma "vela" por venda fechada: sobe se lucro, desce se prejuizo
        {
            "simbolo": v.get("simbolo", "?"),
            "lucro_usd": round(v.get("lucro_usd", 0.0), 4),
            "timestamp": v.get("timestamp"),
        }
        for v in sorted(vendas, key=lambda h: h.get("timestamp", ""))
    ]

    # --- Historico para a tabela (mais recente primeiro) ---------------
    historico_recente = sorted(
        historico, key=lambda h: h.get("timestamp", ""), reverse=True
    )

    return jsonify({
        "dry_run": config.DRY_RUN,
        "cartoes": {
            "saldo_inicial": round(inicial, 2),
            "saldo_livre": round(saldo_livre, 2),
            "valor_posicoes": round(valor_posicoes, 2),
            "valor_total": round(valor_total, 2),
            "lucro_total": round(lucro_total, 2),
            "lucro_total_pct": round(lucro_total_pct, 2),
            "n_compras": len(compras),
            "n_vendas": len(vendas),
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
        },
        "curva_saldo": curva,
        "ganhos_perdas": {
            "total_ganho": round(total_ganho, 2),
            "total_perdido": round(total_perdido, 2),
            "velas": velas,
        },
        "historico": historico_recente,
    })


@app.route("/api/posicoes")
def api_posicoes():
    """Devolve as posicoes abertas com o valor atual estimado (Jupiter).
    Rota separada do /api/resumo porque as cotacoes podem demorar uns
    segundos - assim os cartoes e graficos carregam logo."""
    posicoes = _ler_posicoes()
    lista = []
    for mint, p in posicoes.items():
        quantidade = p.get("quantidade_tokens", 0.0)
        investido = p.get("valor_investido_usd", 0.0)

        valor_atual = _valor_atual_usd(mint, quantidade) if quantidade else None

        # Lucro nao realizado = quanto valeria se vendesses agora - investido
        lucro_nao_realizado = (
            round(valor_atual - investido, 2) if valor_atual is not None else None
        )

        lista.append({
            "mint": mint,
            "simbolo": p.get("simbolo", "?"),
            "valor_investido_usd": investido,
            "preco_compra_usd": p.get("preco_compra_usd", 0.0),
            "quantidade_tokens": quantidade,
            "timestamp_compra": p.get("timestamp_compra"),
            "dry_run": p.get("dry_run", True),
            "valor_atual_usd": round(valor_atual, 2) if valor_atual is not None else None,
            "lucro_nao_realizado_usd": lucro_nao_realizado,
        })

    # Mais recente primeiro, como na tabela de historico
    lista.sort(key=lambda p: p.get("timestamp_compra") or "", reverse=True)
    return jsonify({"posicoes": lista})


if __name__ == "__main__":
    modo = "DRY RUN (simulado)" if config.DRY_RUN else "REAL"
    print(f"Dashboard do bot - modo {modo}")
    print("Aberto em: http://localhost:5000")
    # host="0.0.0.0" permite abrir tambem no browser do telemovel
    # (ex: http://IP-do-dispositivo:5000) se estiverem na mesma rede.
    # debug=False porque isto pode ficar sempre a correr ao lado do bot.
    app.run(host="0.0.0.0", port=5000, debug=False)
