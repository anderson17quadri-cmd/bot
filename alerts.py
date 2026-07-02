"""
alerts.py
=========
Responsavel por MOSTRAR o alerta no terminal, de forma bonita e legivel.

Usa a biblioteca "rich" para cores e caixas. A cor da caixa depende do
score final de risco:
    0-30   -> verde   (parece seguro)
    31-69  -> amarelo (ambiguo / a considerar com cuidado)
    70-100 -> vermelho (muitas red flags)

Este ficheiro NAO decide nada sobre trades. So apresenta informacao.
"""

from datetime import datetime

from rich.console import Console
from rich.panel import Panel

# Uma unica "consola" partilhada para imprimir com cores
console = Console()


def _classificar(score: int) -> tuple[str, str]:
    """A partir do score, devolve (rotulo_de_texto, cor_do_rich)."""
    if score <= 30:
        return "BAIXO RISCO", "green"
    if score < 70:
        return "AMBIGUO / A CONSIDERAR", "yellow"
    return "ALTO RISCO", "red"


def _curto(endereco: str) -> str:
    """Encurta um endereco longo: 'ABC12345....wXYZ' (mais facil de ler)."""
    if not endereco or len(endereco) < 12:
        return endereco or "?"
    return f"{endereco[:6]}...{endereco[-4:]}"


def mostrar_alerta(dados: dict, analise_ia: dict) -> None:
    """Imprime o alerta de um token.

    Parametros:
      dados      - dicionario vindo do analyzer.analisar_onchain(...)
      analise_ia - dicionario montado pelo main.py com o resultado das camadas:
                   {
                     "score_final": int,
                     "fonte_score": str,          # "heuristico" | "DeepSeek" | "Claude"
                     "camada1": {"score", "justificacao"} | None,
                     "camada2": {"score", "justificacao"} | None,
                   }
    """
    score_final = analise_ia["score_final"]
    rotulo, cor = _classificar(score_final)

    hora = datetime.now().strftime("%H:%M:%S")

    # ----- Construir o texto (com marcacao rich, ex: [bold]...[/bold]) -----
    linhas = []
    linhas.append(f"[bold]{dados['token_simbolo']}[/bold]   [dim]({dados['nome_par']})[/dim]")
    linhas.append(f"mint     : {dados['token_mint']}")
    linhas.append(f"dex      : {dados['dex']}   |   idade: {dados['idade_minutos']} min")
    linhas.append(f"liquidez : ${dados['liquidez_usd']:,.0f}")
    if dados.get("fdv_usd"):
        linhas.append(f"fdv      : ${dados['fdv_usd']:,.0f}")

    # Autoridades (o dado mais importante de red flag)
    linhas.append("")
    if dados["onchain_disponivel"]:
        mint_txt = "revogada [green](OK)[/green]" if dados["mint_authority"] is None else f"[red]ATIVA[/red] ({_curto(dados['mint_authority'])})"
        freeze_txt = "revogada [green](OK)[/green]" if dados["freeze_authority"] is None else f"[red]ATIVA[/red] ({_curto(dados['freeze_authority'])})"
        linhas.append(f"mint authority   : {mint_txt}")
        linhas.append(f"freeze authority : {freeze_txt}")
        if dados.get("supply") is not None:
            linhas.append(f"supply           : {dados['supply']:,.0f}")
    else:
        linhas.append("[dim]dados on-chain indisponiveis (RPC limitou)[/dim]")

    # Holders
    if dados["holders_disponivel"]:
        linhas.append(f"maior holder     : {dados['top_holder_pct']:.1f}%")
    else:
        linhas.append("[dim]holders indisponiveis (RPC publico)[/dim]")

    # Fatores de risco (a lista legivel do analyzer)
    linhas.append("")
    linhas.append("[bold]Fatores:[/bold]")
    for f in dados["fatores_risco"]:
        linhas.append(f"  - {f}")

    # ----- Bloco dos scores -----
    linhas.append("")
    linhas.append(f"[bold]Score heuristico[/bold] : {dados['score_heuristico']}/100")

    c1 = analise_ia.get("camada1")
    if c1:
        linhas.append(f"[bold]Camada 1 (DeepSeek)[/bold]: {c1['score']}/100 - {c1['justificacao']}")

    c2 = analise_ia.get("camada2")
    if c2:
        linhas.append(f"[bold]Camada 2 (Claude)[/bold]  : {c2['score']}/100 - {c2['justificacao']}")

    # ----- Titulo e rodape do painel -----
    titulo = f"[{cor}][bold]{rotulo}  ({score_final}/100)[/bold][/{cor}]"
    rodape = f"[dim]{hora}  |  score via {analise_ia.get('fonte_score', '?')}[/dim]"

    painel = Panel(
        "\n".join(linhas),
        title=titulo,
        subtitle=rodape,
        border_style=cor,
        padding=(1, 2),
    )
    console.print(painel)


def info(msg: str) -> None:
    """Mensagem informativa simples (usada pelo main.py no arranque/ciclo)."""
    console.print(f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim] {msg}")


# --------------------------------------------------------------------------
# Teste rapido:  python alerts.py
# Mostra 3 alertas FALSOS para veres as 3 cores (verde/amarelo/vermelho).
# --------------------------------------------------------------------------
if __name__ == "__main__":
    console.print("[bold]Demonstracao de alertas (dados falsos):[/bold]\n")

    # 1) Token que parece seguro (verde)
    seguro = {
        "token_simbolo": "SAFE", "token_mint": "So11111111111111111111111111111111111111112",
        "dex": "raydium", "nome_par": "SAFE / SOL", "liquidez_usd": 85000, "fdv_usd": 500000,
        "idade_minutos": 3.2, "onchain_disponivel": True, "mint_authority": None,
        "freeze_authority": None, "supply": 1_000_000_000, "decimais": 9,
        "holders_disponivel": True, "top_holders": [], "top_holder_pct": 8.5,
        "score_heuristico": 0,
        "fatores_risco": ["Freeze authority revogada (bom sinal)", "Mint authority revogada (bom sinal)", "Liquidez razoavel: $85,000"],
    }
    mostrar_alerta(seguro, {
        "score_final": 12, "fonte_score": "DeepSeek",
        "camada1": {"score": 12, "justificacao": "Autoridades revogadas e boa liquidez."},
        "camada2": None,
    })

    # 2) Token ambiguo (amarelo) -> passou pela Camada 2
    ambiguo = {
        "token_simbolo": "MEH", "token_mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "dex": "pump-fun", "nome_par": "MEH / SOL", "liquidez_usd": 3500, "fdv_usd": 40000,
        "idade_minutos": 0.6, "onchain_disponivel": True, "mint_authority": None,
        "freeze_authority": None, "supply": 1_000_000_000, "decimais": 6,
        "holders_disponivel": False, "top_holders": [], "top_holder_pct": 0.0,
        "score_heuristico": 30,
        "fatores_risco": ["Mint authority revogada (bom sinal)", "Liquidez media: $3,500", "Distribuicao de holders INDISPONIVEL (RPC publico limita)"],
    }
    mostrar_alerta(ambiguo, {
        "score_final": 55, "fonte_score": "Claude",
        "camada1": {"score": 48, "justificacao": "Liquidez baixa mas autoridades ok."},
        "camada2": {"score": 55, "justificacao": "Risco medio; falta info de holders para confirmar."},
    })

    # 3) Token perigoso (vermelho)
    perigoso = {
        "token_simbolo": "SCAM", "token_mint": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
        "dex": "raydium", "nome_par": "SCAM / SOL", "liquidez_usd": 600, "fdv_usd": 900000,
        "idade_minutos": 0.2, "onchain_disponivel": True,
        "mint_authority": "5xY...aBcD", "freeze_authority": "5xY...aBcD",
        "supply": 1_000_000_000, "decimais": 9,
        "holders_disponivel": True, "top_holders": [], "top_holder_pct": 78.0,
        "score_heuristico": 100,
        "fatores_risco": ["Freeze authority ATIVA (podem congelar carteiras)", "Mint authority ATIVA (podem imprimir mais tokens)", "Holder muito concentrado: 78.0% num so endereco", "Liquidez baixa: $600 (< $2,000)"],
    }
    mostrar_alerta(perigoso, {
        "score_final": 95, "fonte_score": "DeepSeek",
        "camada1": {"score": 95, "justificacao": "Autoridades ativas + holder dono de 78%. Classico rug."},
        "camada2": None,
    })
