"""
momentum.py  -  Sinais de momentum/atividade real de um token (Solana)
============================================================================
Conta compras vs vendas e compradores unicos nas transacoes MAIS RECENTES
de um token, para distinguir "ha gente genuinamente a comprar" de "passou
a checklist tecnica mas ninguem quer isto" - o problema reportado no Modo
Caveira: a checklist binaria (autoridades + liquidez + idade) filtra scams
tecnicos, mas NAO filtra qualidade/interesse real, porque a maioria dos
tokens do pump.fun passa essas 4 condicoes nos primeiros segundos.

Usado em 2 sitios:
  1. main._passa_filtro_qualidade_caveira - ANTES de comprar (momentum
     nos primeiros segundos desde a deteccao)
  2. main.verificar_posicoes - em posicoes JA abertas, para detetar uma
     reversao (vendas a ultrapassar as compras) e sair mais cedo

Como contamos sem precisar de Helius (funciona em qualquer RPC, incluindo
o publico, so mais lento): getSignaturesForAddress no MINT (funciona
sempre - qualquer token tem transacoes que o tocam) + getTransaction para
cada uma, comparando pre/postTokenBalances (a mesma tecnica do copy_trade.py,
generalizada). Uma conta cujo saldo do token SUBIU nessa tx fez uma compra;
desceu = venda. Contamos por ENDERECO (owner), nao por transacao, para
"compradores unicos" fazer sentido.

Custo: e RPC-pesado (1 chamada de assinaturas + N chamadas de transacao).
Por isso limitamos sempre o numero de transacoes inspecionadas
(MOMENTUM_MAX_TX_INSPECIONADAS) e usamos poucas tentativas (falhar rapido
em vez de repetir com backoff) - preferimos "sem dados" a demorar demais.

Filosofia (igual ao resto do bot): NUNCA levanta excecao. Se o RPC falhar,
devolve disponivel=False e quem chama decide o que fazer (tipicamente:
nao bloquear por falta de dados, so nao aplicar aquele filtro especifico).
"""

import config
import rpc

# Nº de transacoes recentes a inspecionar em detalhe (getTransaction cada).
# Cap deliberadamente baixo - isto corre no caminho critico do Caveira
# (antes da compra) e periodicamente por posicao aberta (deteccao de
# reversao) - nao pode custar minutos de RPC por token.
MOMENTUM_MAX_TX_INSPECIONADAS = 20


def analisar_momentum(mint: str, excluir: set[str] | None = None) -> dict:
    """Conta compras/vendas/compradores unicos nas transacoes recentes do
    MINT (nao do pool - o mint e universal, existe sempre, e nao depende
    de sabermos o endereco exato do pool/curva).

    'excluir' e um conjunto de enderecos a NAO contar como comprador/
    vendedor (tipicamente o proprio pool/bonding curve - ele nao e um
    "comprador", e a contraparte de todas as trocas).

    Devolve SEMPRE (nunca levanta excecao):
      {
        "disponivel": bool,          # False = RPC falhou, ignora este sinal
        "transacoes_total": int,     # quantas assinaturas existem no mint
        "transacoes_inspecionadas": int,  # quantas foram mesmo analisadas
        "compras": int,              # nº de tx onde um owner ganhou saldo
        "vendas": int,               # nº de tx onde um owner perdeu saldo
        "compradores_unicos": int,   # enderecos distintos que compraram
      }
    """
    vazio_indisponivel = {
        "disponivel": False, "transacoes_total": 0,
        "transacoes_inspecionadas": 0, "compras": 0, "vendas": 0,
        "compradores_unicos": 0,
    }
    excluir = excluir or set()

    try:
        assinaturas = rpc.rpc_call(
            "getSignaturesForAddress", [mint, {"limit": 1000}], tentativas=2
        )
    except Exception:
        return vazio_indisponivel
    if not isinstance(assinaturas, list):
        return vazio_indisponivel

    total = len(assinaturas)
    # So inspecionamos as N mais recentes com sucesso (ignora as que
    # falharam on-chain - err != None - essas nao movem saldo nenhum)
    a_inspecionar = [
        a.get("signature") for a in assinaturas
        if a.get("err") is None and a.get("signature")
    ][:MOMENTUM_MAX_TX_INSPECIONADAS]

    compras = 0
    vendas = 0
    compradores: set[str] = set()

    for sig in a_inspecionar:
        try:
            tx = rpc.rpc_call("getTransaction", [
                sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ], tentativas=1)
        except Exception:
            continue  # uma tx individual falhar nao invalida as restantes

        try:
            meta = (tx or {}).get("meta") or {}

            def saldos(lista) -> dict:
                acc: dict = {}
                for b in lista or []:
                    if b.get("mint") != mint:
                        continue
                    owner = b.get("owner")
                    if not owner or owner in excluir:
                        continue
                    bruto = (b.get("uiTokenAmount") or {}).get("amount") or "0"
                    try:
                        acc[owner] = acc.get(owner, 0.0) + float(bruto)
                    except (TypeError, ValueError):
                        continue
                return acc

            antes = saldos(meta.get("preTokenBalances"))
            depois = saldos(meta.get("postTokenBalances"))
            owners = set(antes) | set(depois)
            for owner in owners:
                diff = depois.get(owner, 0.0) - antes.get(owner, 0.0)
                if diff > 0:
                    compras += 1
                    compradores.add(owner)
                elif diff < 0:
                    vendas += 1
        except Exception:
            continue  # transacao com formato inesperado - salta, nao rebenta

    return {
        "disponivel": True,
        "transacoes_total": total,
        "transacoes_inspecionadas": len(a_inspecionar),
        "compras": compras,
        "vendas": vendas,
        "compradores_unicos": len(compradores),
    }


# --------------------------------------------------------------------------
# Teste rapido:  python3 momentum.py <mint>
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    mint = sys.argv[1] if len(sys.argv) > 1 else "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    print(f"A analisar momentum de {mint}...\n")
    r = analisar_momentum(mint)
    print(r)
