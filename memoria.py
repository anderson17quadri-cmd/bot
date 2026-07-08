"""
memoria.py  -  Registo append-only para analise semanal do bot
================================================================
Tres ficheiros JSONL (uma linha JSON por evento) na pasta memoria/:

  decisoes.jsonl            - toda a analise de token (comprado/rejeitado)
  trades_fechados.jsonl     - cada posicao fechada (venda total), com P/L
  watchlist_historico.jsonl - cada reavaliacao periodica da watchlist

E estritamente ADITIVO: nenhuma logica de trading depende disto. Todas
as funcoes sao nao-bloqueantes - qualquer falha e engolida com um aviso
no log e NUNCA interrompe uma compra/venda. Os ficheiros sao append-only
de proposito (JSONL): baratos de escrever, faceis de analisar depois
(uma linha = um evento, sem estado partilhado para corromper).
"""

import json
import os
from datetime import datetime, timezone

# Pasta ao lado do codigo (nao depende do cwd de quem arrancou o bot)
PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memoria")

_FICHEIROS = {
    "decisao": "decisoes.jsonl",
    "trade": "trades_fechados.jsonl",
    "watchlist": "watchlist_historico.jsonl",
}


def _registar(tipo: str, dados: dict) -> None:
    """Acrescenta uma linha JSON ao ficheiro do tipo dado. Nunca levanta:
    uma falha aqui nao pode, em caso algum, travar uma compra/venda."""
    try:
        os.makedirs(PASTA, exist_ok=True)
        linha = dict(dados)
        # timestamp automatico se quem chamou nao o definiu
        linha.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        caminho = os.path.join(PASTA, _FICHEIROS[tipo])
        with open(caminho, "a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[memoria] falha ao registar '{tipo}': {e}")


def registar_decisao(dados: dict) -> None:
    """Uma decisao de compra/rejeicao sobre um token analisado."""
    _registar("decisao", dados)


def registar_trade_fechado(dados: dict) -> None:
    """Uma posicao fechada (venda total), com P/L e motivo de saida."""
    _registar("trade", dados)


def registar_watchlist(dados: dict) -> None:
    """Uma reavaliacao periodica de um token da watchlist."""
    _registar("watchlist", dados)


# --------------------------------------------------------------------------
# Teste rapido:  python3 memoria.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    registar_decisao({"token": "TESTE", "chain": "solana", "modo": "normal",
                      "decisao": "rejeitado", "motivo_rejeicao": "teste manual"})
    print(f"Linha de teste escrita em {PASTA}/decisoes.jsonl")
