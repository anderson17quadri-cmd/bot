"""
rpc.py
======
Camada de acesso ao RPC da Solana usando JSON-RPC "cru" (via requests).

Porque assim e nao com a solana-py?
 - Nesta Fase 1 so LEMOS dados da blockchain (nao assinamos transacoes),
   e leituras sao simples pedidos HTTP POST.
 - Evita instalar bibliotecas pesadas (Rust) que podem falhar no Termux.
 - E mais educativo: vais ver exatamente o que se pede e o que volta.

O que e "JSON-RPC"?
 - E so um POST para o URL do RPC com um corpo JSON a dizer:
   { "method": "<nome_do_metodo>", "params": [...] }
 - A resposta vem tambem em JSON com o campo "result".

Metodos da Solana que usamos aqui:
 - getAccountInfo (jsonParsed) -> dados do MINT (autoridades, supply, decimais)
 - getTokenLargestAccounts     -> as maiores contas (holders) de um token
"""

import time

import requests

import config


class RPCError(Exception):
    """Erro proprio para falhas de RPC (facilita apanhar mais tarde)."""
    pass


class RPCRateLimit(RPCError):
    """Erro especifico de 'rate limit' (429).

    Serve para o resto do codigo distinguir "o RPC recusou por excesso de
    pedidos" de outros erros. Assim podemos DEGRADAR com elegancia (ex:
    seguir sem a distribuicao de holders) em vez de rebentar.
    """
    pass


# Quantas vezes voltar a tentar quando o RPC responde 429 (rate limit),
# e quanto esperar entre tentativas (segundos). O RPC PUBLICO limita muito,
# por isso ser "educado" com pausas ajuda bastante.
MAX_TENTATIVAS = 4
ESPERA_BASE = 1.5  # segundos (vai crescendo: 1.5, 3, 6, ...)


def rpc_call(method: str, params: list, timeout: int = 15, tentativas: int = MAX_TENTATIVAS) -> dict:
    """Faz uma chamada JSON-RPC generica e devolve o campo 'result'.

    Parametros:
      method    - nome do metodo Solana (ex: "getAccountInfo")
      params    - lista de parametros que esse metodo espera
      timeout   - segundos a esperar antes de desistir
      tentativas- quantas vezes tentar em caso de 429 (usa-se menos para
                  metodos que o RPC publico bloqueia sempre, p/ nao perder tempo)

    Faz retentativas automaticas em caso de 429 (rate limit), com "backoff"
    (espera cada vez maior). Devolve o conteudo de "result".
    Se esgotar as tentativas ou houver outro erro, levanta RPCError.
    """
    # Corpo do pedido no formato JSON-RPC 2.0
    corpo = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }

    ultimo_erro = ""
    for tentativa in range(1, tentativas + 1):
        try:
            resposta = requests.post(config.SOLANA_RPC_URL, json=corpo, timeout=timeout)
        except requests.RequestException as e:
            # Ex: sem internet, timeout, DNS... tentamos de novo
            ultimo_erro = f"Falha de rede: {e}"
            time.sleep(ESPERA_BASE * tentativa)
            continue

        # 429 = "Too Many Requests" -> esperamos mais um pouco e tentamos de novo
        if resposta.status_code == 429:
            espera = ESPERA_BASE * (2 ** (tentativa - 1))  # 1.5, 3, 6, 12...
            ultimo_erro = "429 (rate limit)"
            time.sleep(espera)
            continue

        if resposta.status_code != 200:
            raise RPCError(f"RPC devolveu HTTP {resposta.status_code} em {method}")

        dados = resposta.json()

        # O RPC tambem pode devolver o erro DENTRO do JSON (com HTTP 200).
        # Ex: {"error": {"code": 429, "message": "Too many requests..."}}
        if "error" in dados:
            erro = dados["error"]
            codigo = erro.get("code") if isinstance(erro, dict) else None
            mensagem = str(erro)
            if codigo == 429 or "too many requests" in mensagem.lower():
                espera = ESPERA_BASE * (2 ** (tentativa - 1))
                ultimo_erro = f"429 no corpo ({mensagem})"
                time.sleep(espera)
                continue
            # Outro tipo de erro -> nao vale a pena repetir
            raise RPCError(f"Erro RPC em {method}: {erro}")

        return dados.get("result", {})

    # Se chegamos aqui, esgotaram-se as tentativas -> era rate limit persistente.
    raise RPCRateLimit(
        f"RPC bloqueou {method} apos {tentativas} tentativas ({ultimo_erro}). "
        f"O RPC publico restringe metodos pesados. Dica: usa Helius/QuickNode."
    )


def get_mint_info(mint: str) -> dict | None:
    """Le a informacao "parsed" da conta do MINT de um token.

    Devolve um dicionario simples:
      {
        "mint_authority":   str | None,   # None = ninguem pode imprimir mais (bom sinal)
        "freeze_authority": str | None,   # None = ninguem pode congelar (bom sinal)
        "supply":           float,        # total de tokens em circulacao
        "decimais":         int,
        "inicializado":     bool,
      }
    Devolve None se a conta nao existir ou nao for um token SPL valido.
    """
    # encoding "jsonParsed" pede ao RPC para JA nos dar os campos legiveis,
    # em vez de bytes que teriamos de descodificar a mao.
    resultado = rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])

    valor = resultado.get("value")
    if not valor:
        # Conta nao existe
        return None

    # Confirmar que e mesmo uma conta de token SPL do tipo "mint"
    dados = valor.get("data", {})
    parsed = dados.get("parsed", {}) if isinstance(dados, dict) else {}
    if parsed.get("type") != "mint":
        return None

    info = parsed.get("info", {})

    # supply vem como string de "unidades base"; dividimos por 10^decimais
    decimais = int(info.get("decimals", 0))
    supply_bruto = int(info.get("supply", "0"))
    supply = supply_bruto / (10 ** decimais) if decimais >= 0 else float(supply_bruto)

    return {
        "mint_authority": info.get("mintAuthority"),      # pode ser None
        "freeze_authority": info.get("freezeAuthority"),  # pode ser None
        "supply": supply,
        "decimais": decimais,
        "inicializado": bool(info.get("isInitialized", False)),
    }


def get_maiores_holders(mint: str, supply: float | None = None, limite: int = 20) -> list[dict]:
    """Devolve as maiores contas (holders) de um token e a sua percentagem.

    NOTA IMPORTANTE (para nao te enganares na leitura):
      A maior "conta" costuma ser o COFRE DO POOL de liquidez, nao uma
      pessoa. Por isso, uma percentagem alta no topo nem sempre e mau.
      A IA (Camada 1/2) recebe este aviso e pesa-o na analise.

    Cada item devolvido:
      { "endereco": str, "quantidade": float, "pct": float }

    Usa poucas tentativas de proposito: no RPC publico este metodo esta quase
    sempre bloqueado, logo nao vale a pena perder ~20s a insistir. Com Helius
    passa a funcionar a primeira.
    """
    resultado = rpc_call("getTokenLargestAccounts", [mint], tentativas=2)
    contas = resultado.get("value", []) or []

    holders = []
    for conta in contas[:limite]:
        # uiAmount ja vem convertido com os decimais aplicados
        quantidade = conta.get("uiAmount") or 0.0
        # Percentagem face ao supply total (se soubermos o supply)
        pct = (quantidade / supply * 100.0) if supply and supply > 0 else 0.0
        holders.append({
            "endereco": conta.get("address", "?"),
            "quantidade": float(quantidade),
            "pct": round(pct, 2),
        })

    return holders


def obter_maiores_holders(mint: str, supply: float | None = None) -> list[dict]:
    """Versao "a prova de falha" do get_maiores_holders, pensada para o
    ciclo principal do bot: se QUALQUER coisa correr mal (rate limit,
    RPC em baixo, timeout, resposta estranha), devolve lista vazia em
    vez de levantar excecao - o bot segue em frente sem holders, como
    antes, e nunca rebenta por causa disto.

    Com um RPC como o Helius configurado no .env, o metodo
    getTokenLargestAccounts funciona a primeira e a lista vem cheia
    (ate 20 contas, da maior para a mais pequena).
    """
    try:
        return get_maiores_holders(mint, supply=supply, limite=20)
    except Exception:
        # Apanha tudo: RPCError/RPCRateLimit, falhas de rede, JSON mal
        # formado... a resposta certa e sempre "sem dados", nunca parar o bot
        return []


# --------------------------------------------------------------------------
# Teste rapido:  python rpc.py
# Usa o BONK (token conhecido) para confirmarmos que a leitura funciona.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    print(f"A ler dados on-chain do BONK ({BONK}) ...\n")

    info = get_mint_info(BONK)
    if info is None:
        print("Nao foi possivel ler o mint (conta inexistente?).")
    else:
        print("== MINT INFO ==")
        print(f"  Mint authority   : {info['mint_authority']}  (None = nao podem imprimir mais)")
        print(f"  Freeze authority : {info['freeze_authority']}  (None = nao podem congelar)")
        print(f"  Supply           : {info['supply']:,.0f}")
        print(f"  Decimais         : {info['decimais']}")

        print("\n== TOP 5 HOLDERS ==")
        try:
            top = get_maiores_holders(BONK, supply=info["supply"], limite=5)
            for i, h in enumerate(top, start=1):
                print(f"  {i}. {h['endereco'][:6]}...{h['endereco'][-4:]}  ->  {h['pct']:.2f}%")
        except RPCRateLimit as e:
            # No RPC publico este metodo costuma ser bloqueado. Nao e erro fatal.
            print(f"  [indisponivel] {e}")
