"""
detector.py
===========
Deteta pools/pares NOVOS na Solana.

Estrategia da Fase 1: PERGUNTAR (polling) a uma API gratuita que ja lista os
pools mais recentes -> a GeckoTerminal (endpoint "new_pools").

Porque polling e nao WebSocket em tempo real?
 - Em "modo alerta" a latencia nao interessa (nao estamos a fazer trades).
 - E fiavel e simples, e o RPC publico nem sequer aguenta WebSocket serio.
 - A interface aqui (classe DetectorPools) esta pronta para, na Fase 2 com
   Helius, trocar o "motor" por WebSocket sem mexer no resto do bot.

Nota: a API devolve pools de varios DEX (raydium, pumpswap, orca, meteora...).
E onde os tokens novos aparecem hoje em dia, por isso incluimos todos e
mostramos de qual DEX veio cada um.
"""

from datetime import datetime, timezone

import requests

import config
import html
# Endpoint da GeckoTerminal que lista os pools mais recentes de uma rede.
# Documentacao: https://www.geckoterminal.com/dex-api
URL_NOVOS_POOLS = f"https://api.geckoterminal.com/api/v2/networks/{config.REDE}/new_pools"

# A GeckoTerminal recomenda enviar este cabecalho para "fixar" a versao da API.
CABECALHOS = {"Accept": "application/json;version=20230302"}


def _sem_prefixo_rede(token_id: str) -> str:
    """A API identifica tokens como 'solana_<mint>'. Aqui tiramos o 'solana_'.

    Ex: 'solana_HTN638...' -> 'HTN638...'
    """
    prefixo = f"{config.REDE}_"
    if token_id.startswith(prefixo):
        return token_id[len(prefixo):]
    return token_id


def _idade_minutos(criado_em_iso: str) -> float:
    """Calcula ha quantos minutos o pool foi criado (a partir do timestamp ISO).

    Devolve -1 se nao conseguir interpretar a data.
    """
    if not criado_em_iso:
        return -1.0
    try:
        # A data vem como "2026-07-02T05:26:01Z"; trocamos o Z por +00:00
        criado = datetime.fromisoformat(criado_em_iso.replace("Z", "+00:00"))
        agora = datetime.now(timezone.utc)
        return (agora - criado).total_seconds() / 60.0
    except ValueError:
        return -1.0


def _para_numero(valor) -> float | None:
    """Converte com seguranca para float (a API manda numeros como texto)."""
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _interpretar_pool(pool: dict) -> dict | None:
    """Transforma o JSON cru de UM pool no nosso formato simples.

    Devolve None se faltar informacao essencial (ex: sem token).
    """
    attrs = pool.get("attributes", {})
    rel = pool.get("relationships", {})

    # Mints dos dois lados do par
    base_id = rel.get("base_token", {}).get("data", {}).get("id", "")
    quote_id = rel.get("quote_token", {}).get("data", {}).get("id", "")
    base_mint = _sem_prefixo_rede(base_id)
    quote_mint = _sem_prefixo_rede(quote_id)

    dex = rel.get("dex", {}).get("data", {}).get("id", "?")

    # O "name" costuma ser "SIMBOLO_BASE / SIMBOLO_QUOTE" (ex: "favier / SOL")
    nome_par = html.unescape(attrs.get("name", "?"))
    partes = [p.strip() for p in nome_par.split("/")]
    simbolo_base = partes[0] if len(partes) >= 1 else "?"
    simbolo_quote = partes[1] if len(partes) >= 2 else "?"

    # Descobrir qual lado e o TOKEN NOVO:
    # normalmente o lado "base" e o token novo e o "quote" e SOL/USDC.
    # Mas se por acaso for ao contrario, invertemos.
    if base_mint in config.MINTS_BASE_CONHECIDOS and quote_mint not in config.MINTS_BASE_CONHECIDOS:
        token_mint, token_simbolo = quote_mint, simbolo_quote
        contra_mint, contra_simbolo = base_mint, simbolo_base
    else:
        token_mint, token_simbolo = base_mint, simbolo_base
        contra_mint, contra_simbolo = quote_mint, simbolo_quote

    # Sem mint do token nao ha nada a analisar
    if not token_mint:
        return None

    return {
        "pool_address": attrs.get("address", "?"),
        "dex": dex,
        "nome_par": nome_par,
        "token_mint": token_mint,
        "token_simbolo": token_simbolo,
        "contra_mint": contra_mint,      # o outro lado (SOL/USDC)
        "contra_simbolo": contra_simbolo,
        "liquidez_usd": _para_numero(attrs.get("reserve_in_usd")) or 0.0,
        "fdv_usd": _para_numero(attrs.get("fdv_usd")),
        "market_cap_usd": _para_numero(attrs.get("market_cap_usd")),
        "criado_em": attrs.get("pool_created_at", ""),
        "idade_minutos": round(_idade_minutos(attrs.get("pool_created_at", "")), 1),
    }


def buscar_pools_crus(pagina: int = 1) -> list[dict]:
    """Vai a API buscar a lista de pools recentes e devolve-a ja interpretada.

    NAO faz filtragem de "ja vistos" - isso e a classe DetectorPools que trata.
    Levanta requests.RequestException se a rede falhar (quem chama decide).
    """
    resposta = requests.get(
        URL_NOVOS_POOLS,
        headers=CABECALHOS,
        params={"page": pagina},
        timeout=20,
    )
    resposta.raise_for_status()
    dados = resposta.json().get("data", [])

    pools = []
    for pool in dados:
        interpretado = _interpretar_pool(pool)
        if interpretado is not None:
            pools.append(interpretado)
    return pools


class DetectorPools:
    """Guarda quais os pools ja vistos e devolve apenas os NOVOS a cada chamada.

    Uso tipico (em main.py):
        det = DetectorPools()
        while True:
            for pool in det.buscar_novos():
                analisar(pool)
            time.sleep(config.POLL_INTERVAL_SEGUNDOS)
    """

    def __init__(self, emitir_no_arranque: int = 3):
        # Conjunto de enderecos de pool ja processados (para nao repetir alertas)
        self._vistos: set[str] = set()
        # Na primeira vez, quantos pools recentes emitir logo (para veres
        # output imediato no arranque). Depois disso, so emite os genuinamente novos.
        self._emitir_no_arranque = emitir_no_arranque
        self._primeira_vez = True

    def buscar_novos(self) -> list[dict]:
        """Devolve a lista de pools que ainda nao tinhamos visto.

        Se houver falha de rede, devolve lista vazia (nao rebenta o ciclo).
        """
        try:
            pools = buscar_pools_crus()
        except requests.RequestException as e:
            print(f"[detector] aviso: falha ao buscar pools ({e})")
            return []

        novos = []
        for pool in pools:
            endereco = pool["pool_address"]
            if endereco in self._vistos:
                continue
            self._vistos.add(endereco)
            novos.append(pool)

        # No arranque, para nao despejar 20 pools antigos de uma vez,
        # devolvemos so os N mais recentes (a API ja vem por ordem de recencia).
        if self._primeira_vez:
            self._primeira_vez = False
            return novos[: self._emitir_no_arranque]

        return novos


# --------------------------------------------------------------------------
# Teste rapido:  python detector.py
# Mostra os pools mais recentes que a API devolve agora.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("A procurar pools recentes na Solana (GeckoTerminal)...\n")
    try:
        pools = buscar_pools_crus()
    except requests.RequestException as e:
        print(f"Falha de rede: {e}")
        raise SystemExit(1)

    print(f"Encontrados {len(pools)} pools. A mostrar os 8 mais recentes:\n")
    for p in pools[:8]:
        print(f"- [{p['dex']}] {p['nome_par']}")
        print(f"    token : {p['token_simbolo']}  ({p['token_mint']})")
        print(f"    liquidez: ${p['liquidez_usd']:,.0f}  | idade: {p['idade_minutos']} min")
        print()
