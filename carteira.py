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


def _sanidade_ok(valor_usd: float, operacao: str, simbolo: str) -> bool:
    """Rede de seguranca contra bugs de unidades (ex: uma quantidade de
    tokens - um numero na casa dos milhares de milhoes - usada por engano
    como se fosse valor em USD). Ja aconteceu: saldo simulado saltou de
    $200 para $212,009 numa unica operacao.

    Rejeita QUALQUER compra/venda cujo valor absoluto exceda
    config.limite_sanidade_trade_usd() (por defeito, 50x o maior limite
    de trade configurado entre os 3 modos - generoso, so apanha
    corrupcoes de ordens de grandeza, nunca trades legitimos)."""
    limite = config.limite_sanidade_trade_usd()
    if abs(valor_usd) <= limite:
        return True
    print(
        f"[carteira] ERRO DE SANIDADE: {operacao} de {simbolo} rejeitada - "
        f"valor ${valor_usd:,.2f} excede o limite de seguranca (${limite:,.2f}, "
        f"50x o maior MAX_TRADE_USD configurado). Isto cheira a bug de unidades "
        f"(preco/quantidade trocados) - a operacao NAO foi aplicada ao saldo."
    )
    return False


def registar_compra(simbolo: str, valor_usd: float, mint: str | None = None,
                    preco_unitario_usd: float | None = None,
                    quantidade_tokens: float | None = None,
                    chain: str = "solana",
                    dex: str | None = None,
                    modo: str = "normal") -> bool:
    """Debita o valor da compra do saldo virtual. Devolve False (e nao
    debita nada) se nao houver saldo suficiente OU se o valor falhar a
    verificacao de sanidade (protecao contra bugs de unidades).

    Os campos extra sao opcionais (registos antigos nao os tem):
      mint               -> para o dashboard abrir o grafico do token
      preco_unitario_usd -> preco pago por unidade minima do token
      quantidade_tokens  -> quantas unidades minimas foram compradas
      dex                -> plataforma/DEX de origem (pump-fun, raydium...)
      modo               -> qual dos 4 modos de compra fez esta operacao
                            (normal|bonding_curve|sniper_rapido|copy_trading)
                            - usado pelas estatisticas "por modo"/"por DEX"
    Assim o historico fica completo mesmo depois de a posicao fechar."""
    if not _sanidade_ok(valor_usd, "compra", simbolo):
        return False

    dados = _carregar()
    if dados["saldo_atual_usd"] < valor_usd:
        return False

    dados["saldo_atual_usd"] -= valor_usd
    dados["historico"].append({
        "tipo": "compra", "simbolo": simbolo, "valor_usd": valor_usd,
        "mint": mint, "chain": chain, "dex": dex, "modo": modo,
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
                   chain: str = "solana",
                   sniper_rapido: bool = False,
                   dex: str | None = None,
                   modo: str | None = None) -> bool:
    """Credita o valor recebido da venda no saldo virtual e regista o
    lucro/prejuizo realizado dessa operacao. Devolve False (e nao mexe
    no saldo) se o valor falhar a verificacao de sanidade - protecao
    contra bugs de unidades (ex: cotacao de um pool manipulado/ilíquido
    a devolver um numero absurdo).

    IMPORTANTE: se isto devolver False, quem chamou NAO deve fechar nem
    reduzir a posicao - o "dinheiro" simulado nunca chegou a entrar, por
    isso a posicao continua aberta para tentares vender outra vez depois.

    Os campos extra (opcionais) preservam o que a posicao sabia ANTES
    de ser apagada pelo fechar_posicao(): o mint, o preco a que se
    comprou, o preco a que se vendeu e a quantidade vendida - sem isto,
    fechada a posicao, esses dados perdiam-se para sempre.

    'dex'/'modo': mesmo par usado em registar_compra, para as estatisticas
    do dashboard conseguirem juntar compra+venda do mesmo modo/plataforma.
    'modo' sem valor explicito e deduzido de 'sniper_rapido' (compat com
    chamadas antigas que so passavam esse booleano)."""
    if not _sanidade_ok(valor_recebido_usd, "venda", simbolo):
        return False

    if modo is None:
        modo = "sniper_rapido" if sniper_rapido else "normal"

    dados = _carregar()
    lucro = valor_recebido_usd - valor_investido_usd
    dados["saldo_atual_usd"] += valor_recebido_usd
    dados["historico"].append({
        "tipo": "venda", "simbolo": simbolo,
        "valor_usd": valor_recebido_usd, "lucro_usd": round(lucro, 4),
        "mint": mint, "chain": chain, "dex": dex, "modo": modo,
        "preco_compra_usd": preco_compra_usd,
        "preco_venda_usd": preco_venda_usd,
        "quantidade_tokens": quantidade_tokens,
        "sniper_rapido": sniper_rapido,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _guardar(dados)
    return True


def marcar_ultima_compra(mint: str, **campos) -> None:
    """Acrescenta campos extra a entrada de COMPRA mais recente de um
    mint (ex: sniper_rapido=True). Usado por modos de compra dedicados
    (ex: sniper_rapido.py) que reutilizam o executor.comprar_token
    partilhado mas querem marcar a origem SO no seu proprio codigo, sem
    mexer na assinatura da funcao de compra partilhada."""
    dados = _carregar()
    for h in reversed(dados["historico"]):
        if h.get("tipo") == "compra" and h.get("mint") == mint:
            h.update(campos)
            _guardar(dados)
            return


def remover_do_historico(timestamp: str) -> bool:
    """Remove UMA entrada do historico pelo seu timestamp (identificador
    estavel - ao contrario do indice, que muda com a ordenacao no ecra).

    NAO mexe no saldo: e so limpeza visual do historico, a pedido do
    utilizador. Devolve True se removeu algo, False se nao encontrou.
    """
    dados = _carregar()
    antes = len(dados["historico"])
    dados["historico"] = [h for h in dados["historico"] if h.get("timestamp") != timestamp]
    if len(dados["historico"]) == antes:
        return False  # nao encontrou nenhuma entrada com esse timestamp
    _guardar(dados)
    return True


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

