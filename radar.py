"""
radar.py  -  Registo dos tokens detetados e analisados (o "radar")
====================================================================
Diferente do posicoes.py (que guarda so o que o bot COMPROU), este
modulo guarda TODOS os tokens que o detector encontrou e o analyzer
avaliou - comprados ou nao. E a materia-prima da seccao "Radar ao
vivo" do dashboard: uma janela para o que esta a acontecer no mercado.

Ficheiro local: radar.json - uma LISTA (mais recente primeiro),
limitada aos MAX_REGISTOS ultimos para nao crescer para sempre:
[
    {
        "simbolo": "XYZ",
        "mint": "Abc123...",
        "dex": "raydium",
        "idade_minutos": 4.2,
        "liquidez_usd": 12500.0,
        "score": 35,
        "fonte_score": "DeepSeek",
        "comprado": false,
        "timestamp": "2026-07-02T21:00:00+00:00"
    },
    ...
]

Escrita segura: mesmo padrao do posicoes.py (escrever para um .tmp e
so depois fazer os.replace) - assim, mesmo que o bot seja morto a meio
da escrita, o radar.json nunca fica corrompido.
"""

import json
import os
from datetime import datetime, timezone

FICHEIRO_RADAR = "radar.json"
MAX_REGISTOS = 50  # guarda so os 50 tokens mais recentes


def carregar_radar() -> list:
    """Le o radar.json. Se nao existir ou estiver corrompido, devolve
    uma lista vazia em vez de rebentar."""
    if not os.path.exists(FICHEIRO_RADAR):
        return []
    try:
        with open(FICHEIRO_RADAR, "r", encoding="utf-8") as f:
            dados = json.load(f)
            # Garante que e mesmo uma lista (protege contra edicao manual)
            return dados if isinstance(dados, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _guardar(registos: list) -> None:
    """Escrita atomica: primeiro para um ficheiro temporario, depois
    troca (rename). Nunca deixa o radar.json meio-escrito."""
    tmp = FICHEIRO_RADAR + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registos, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FICHEIRO_RADAR)


def registar_analise(dados: dict, analise_ia: dict, comprado: bool) -> None:
    """Acrescenta um token analisado ao topo do radar.

    'dados'      -> o dicionario do analyzer.analisar_onchain()
    'analise_ia' -> o resultado do avaliar_com_ia() do main.py
    'comprado'   -> True se o bot decidiu entrar neste token
    """
    registos = carregar_radar()

    registos.insert(0, {  # insert(0, ...) = poe no INICIO (mais recente primeiro)
        "simbolo": dados.get("token_simbolo", "?"),
        "mint": dados.get("token_mint", ""),
        "dex": dados.get("dex", "?"),
        "idade_minutos": dados.get("idade_minutos"),
        "liquidez_usd": dados.get("liquidez_usd", 0.0),
        "score": analise_ia.get("score_final", 0),
        "fonte_score": analise_ia.get("fonte_score", "heuristico"),
        "comprado": comprado,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Corta a lista aos MAX_REGISTOS mais recentes
    _guardar(registos[:MAX_REGISTOS])


# --------------------------------------------------------------------------
# Teste rapido:  python3 radar.py
# Mostra o que esta gravado no radar neste momento.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    registos = carregar_radar()
    print(f"Radar: {len(registos)} token(s) registado(s)")
    for r in registos[:10]:
        tag = " [COMPRADO]" if r["comprado"] else ""
        print(f"  {r['simbolo']:<10} score {r['score']:>3}  "
              f"liq ${r['liquidez_usd']:>10,.0f}  {r['timestamp']}{tag}")
