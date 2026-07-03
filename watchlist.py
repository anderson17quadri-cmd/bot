"""
watchlist.py  -  Tokens "fronteira" para decisao manual
=========================================================
Quando um token tem score DEMASIADO ALTO para compra automatica mas
por pouco (entre SCORE_COMPRA_MAX e SCORE_COMPRA_MAX +
SCORE_WATCHLIST_MARGEM), nao e comprado - mas tambem nao e esquecido:
entra nesta watchlist, onde continua a ser reavaliado a cada ciclo
(preco atual, liquidez ainda viva?) para o utilizador poder decidir
comprar manualmente pelo dashboard.

Ficheiro local: watchlist.json - lista (mais recente primeiro):
[
    {
        "mint": "...", "simbolo": "XYZ", "dex": "raydium",
        "score": 32, "confianca": 75, "liquidez_usd": 8000.0,
        "liquidez_bloqueada": "queimada",
        "seguido": false,          <- true = utilizador clicou "Seguir"
        "adicionado_em": "...",
        "reavaliacao": {           <- atualizado a cada ciclo pelo main.py
            "preco_unitario_usd": 0.0000012,
            "liquidez_viva": true,
            "timestamp": "..."
        } | null
    },
    ...
]

Regras de manutencao:
 - Maximo MAX_REGISTOS entradas; quando passa, saem as mais ANTIGAS
   NAO seguidas (as "seguidas" ficam - e esse o valor do botao Seguir)
 - Escrita atomica (tmp + rename), como posicoes.py/radar.py
"""

import json
import os
from datetime import datetime, timezone

FICHEIRO_WATCHLIST = "watchlist.json"
MAX_REGISTOS = 30


def carregar_watchlist() -> list:
    """Le o watchlist.json. Ficheiro em falta/corrompido -> lista vazia."""
    if not os.path.exists(FICHEIRO_WATCHLIST):
        return []
    try:
        with open(FICHEIRO_WATCHLIST, "r", encoding="utf-8") as f:
            dados = json.load(f)
            return dados if isinstance(dados, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _guardar(registos: list) -> None:
    """Escrita atomica: tmp + rename, nunca deixa o ficheiro meio-escrito."""
    tmp = FICHEIRO_WATCHLIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registos, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FICHEIRO_WATCHLIST)


def _podar(registos: list) -> list:
    """Aplica o limite MAX_REGISTOS: remove do FIM (mais antigos) as
    entradas nao seguidas ate caber. Entradas 'seguidas' nunca saem."""
    if len(registos) <= MAX_REGISTOS:
        return registos
    resultado = list(registos)
    # Percorre de tras para a frente a remover nao-seguidos ate caber.
    # (Se sobrarem mais de MAX_REGISTOS todos seguidos, ficam todos -
    #  o utilizador pediu explicitamente para os seguir.)
    for i in range(len(resultado) - 1, -1, -1):
        if len(resultado) <= MAX_REGISTOS:
            break
        if not resultado[i].get("seguido"):
            resultado.pop(i)
    return resultado


def adicionar(dados: dict, analise_ia: dict) -> None:
    """Adiciona um token fronteira ao topo da watchlist.
    Se o mint ja la estiver, atualiza o score e nao duplica."""
    registos = carregar_watchlist()
    mint = dados.get("token_mint", "")
    camada1 = analise_ia.get("camada1") or {}

    # Ja existe? Atualiza os campos de analise e traz para o topo
    existente = next((r for r in registos if r.get("mint") == mint), None)
    if existente:
        registos.remove(existente)
        existente["score"] = analise_ia.get("score_final", existente.get("score"))
        existente["confianca"] = camada1.get("confianca", existente.get("confianca"))
        existente["liquidez_usd"] = dados.get("liquidez_usd", existente.get("liquidez_usd"))
        registos.insert(0, existente)
    else:
        registos.insert(0, {
            "mint": mint,
            "simbolo": dados.get("token_simbolo", "?"),
            "dex": dados.get("dex", "?"),
            "score": analise_ia.get("score_final", 0),
            "confianca": camada1.get("confianca"),
            "liquidez_usd": dados.get("liquidez_usd", 0.0),
            "liquidez_bloqueada": dados.get("liquidez_bloqueada", "desconhecido"),
            "seguido": False,
            "adicionado_em": datetime.now(timezone.utc).isoformat(),
            "reavaliacao": None,
        })

    _guardar(_podar(registos))


def marcar_seguir(mint: str, seguir: bool = True) -> bool:
    """Marca/desmarca um token como 'seguido' (o botao Seguir do
    dashboard). Devolve False se o mint nao estiver na watchlist."""
    registos = carregar_watchlist()
    for r in registos:
        if r.get("mint") == mint:
            r["seguido"] = bool(seguir)
            _guardar(registos)
            return True
    return False


def atualizar_reavaliacao(mint: str, preco_unitario_usd: float | None,
                          liquidez_viva: bool) -> None:
    """Guarda o resultado da reavaliacao periodica de um token (feita
    pelo main.py a cada ciclo com cotacoes Jupiter)."""
    registos = carregar_watchlist()
    for r in registos:
        if r.get("mint") == mint:
            r["reavaliacao"] = {
                "preco_unitario_usd": preco_unitario_usd,
                "liquidez_viva": liquidez_viva,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _guardar(registos)
            return


def remover(mint: str) -> None:
    """Tira um token da watchlist (ex: depois de comprado manualmente)."""
    registos = [r for r in carregar_watchlist() if r.get("mint") != mint]
    _guardar(registos)


# --------------------------------------------------------------------------
# Teste rapido:  python3 watchlist.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    registos = carregar_watchlist()
    print(f"Watchlist: {len(registos)} token(s)")
    for r in registos:
        pin = " [SEGUIDO]" if r.get("seguido") else ""
        print(f"  {r['simbolo']:<10} score {r['score']:>3}  "
              f"liq ${r['liquidez_usd']:>10,.0f}{pin}")
