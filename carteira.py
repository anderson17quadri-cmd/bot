"""
carteira.py  -  Carteira VIRTUAL para o modo de teste (dry-run)
==================================================================
Simula um saldo em USD que sobe e desce com as compras/vendas simuladas,
para no final poderes ver quanto "ganhaste" ou "perdeste" com dinheiro
falso, antes de arriscares dinheiro real.

So e usada quando config.DRY_RUN == True. Em modo real, o saldo verdadeiro
vem da wallet (wallet.obter_saldo_sol()), nao daqui.

Ficheiro local: carteira.json
{
    "saldo_inicial_usd": 100.0,
    "saldo_atual_usd": 92.4,
    "historico": [
        {"tipo": "compra", "simbolo": "XYZ", "valor_usd": 5.0, "timestamp": "..."},
        {"tipo": "venda", "simbolo": "XYZ", "valor_usd": 6.2, "lucro_usd": 1.2, "timestamp": "..."},
        ...
    ]
}
"""

import json
import os
from datetime import datetime, timezone

import config

FICHEIRO_CARTEIRA = "carteira.json"


def _carregar() -> dict:
    if not os.path.exists(FICHEIRO_CARTEIRA):
        return {
            "saldo_inicial_usd": config.SALDO_VIRTUAL_INICIAL,
            "saldo_atual_usd": config.SALDO_VIRTUAL_INICIAL,
            "historico": [],
        }
    try:
        with open(FICHEIRO_CARTEIRA, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "saldo_inicial_usd": config.SALDO_VIRTUAL_INICIAL,
            "saldo_atual_usd": config.SALDO_VIRTUAL_INICIAL,
            "historico": [],
        }


def _guardar(dados: dict) -> None:
    tmp = FICHEIRO_CARTEIRA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FICHEIRO_CARTEIRA)


def saldo_disponivel() -> float:
    """Saldo virtual atual (USD) disponivel para novas compras."""
    return _carregar()["saldo_atual_usd"]


def registar_compra(simbolo: str, valor_usd: float, mint: str | None = None,
                    preco_unitario_usd: float | None = None,
                    quantidade_tokens: float | None = None,
                    chain: str = "solana") -> bool:
    """Debita o valor da compra do saldo virtual. Devolve False (e nao
    debita nada) se nao houver saldo suficiente.

    Os campos extra sao opcionais (registos antigos nao os tem):
      mint               -> para o dashboard abrir o grafico do token
      preco_unitario_usd -> preco pago por unidade minima do token
      quantidade_tokens  -> quantas unidades minimas foram compradas
    Assim o historico fica completo mesmo depois de a posicao fechar."""
    dados = _carregar()
    if dados["saldo_atual_usd"] < valor_usd:
        return False

    dados["saldo_atual_usd"] -= valor_usd
    dados["historico"].append({
        "tipo": "compra", "simbolo": simbolo, "valor_usd": valor_usd,
        "mint": mint, "chain": chain,
        "preco_unitario_usd": preco_unitario_usd,
        "quantidade_tokens": quantidade_tokens,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _guardar(dados)
    return True


def registar_venda(simbolo: str, valor_recebido_usd: float, valor_investido_usd: float,
                   mint: str | None = None,
                   preco_compra_usd: float | None = None,
                   preco_venda_usd: float | None = None,
                   quantidade_tokens: float | None = None,
                   chain: str = "solana") -> None:
    """Credita o valor recebido da venda no saldo virtual e regista o
    lucro/prejuizo realizado dessa operacao.

    Os campos extra (opcionais) preservam o que a posicao sabia ANTES
    de ser apagada pelo fechar_posicao(): o mint, o preco a que se
    comprou, o preco a que se vendeu e a quantidade vendida - sem isto,
    fechada a posicao, esses dados perdiam-se para sempre."""
    dados = _carregar()
    lucro = valor_recebido_usd - valor_investido_usd
    dados["saldo_atual_usd"] += valor_recebido_usd
    dados["historico"].append({
        "tipo": "venda", "simbolo": simbolo,
        "valor_usd": valor_recebido_usd, "lucro_usd": round(lucro, 4),
        "mint": mint, "chain": chain,
        "preco_compra_usd": preco_compra_usd,
        "preco_venda_usd": preco_venda_usd,
        "quantidade_tokens": quantidade_tokens,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _guardar(dados)


def relatorio(valor_posicoes_abertas_usd: float = 0.0) -> str:
    """Gera um relatorio de texto com o resultado do modo de teste ate agora.

    'valor_posicoes_abertas_usd' e o valor ATUAL (a mercado) de tudo o que
    ainda esta investido e por vender - passa isto se quiseres o lucro/
    prejuizo TOTAL (realizado + nao realizado). Se nao passares nada, o
    relatorio mostra so o realizado (dinheiro que ja voltou para o saldo).
    """
    dados = _carregar()
    inicial = dados["saldo_inicial_usd"]
    atual = dados["saldo_atual_usd"]

    lucro_realizado = sum(
        h.get("lucro_usd", 0) for h in dados["historico"] if h["tipo"] == "venda"
    )
    n_compras = sum(1 for h in dados["historico"] if h["tipo"] == "compra")
    n_vendas = sum(1 for h in dados["historico"] if h["tipo"] == "venda")

    valor_total_estimado = atual + valor_posicoes_abertas_usd
    lucro_total = valor_total_estimado - inicial
    lucro_total_pct = (lucro_total / inicial * 100) if inicial else 0

    linhas = [
        "=" * 50,
        "RELATORIO DO MODO DE TESTE (dinheiro simulado)",
        "=" * 50,
        f"Saldo inicial            : ${inicial:.2f}",
        f"Saldo livre atual        : ${atual:.2f}",
        f"Valor em posicoes abertas: ${valor_posicoes_abertas_usd:.2f}",
        f"Valor total estimado     : ${valor_total_estimado:.2f}",
        "-" * 50,
        f"Compras realizadas       : {n_compras}",
        f"Vendas realizadas        : {n_vendas}",
        f"Lucro/prejuizo REALIZADO : ${lucro_realizado:+.2f}",
        f"Lucro/prejuizo TOTAL     : ${lucro_total:+.2f} ({lucro_total_pct:+.1f}%)",
        "=" * 50,
    ]
    return "\n".join(linhas)


if __name__ == "__main__":
    print(relatorio())

