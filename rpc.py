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
import rate_limiter


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

# Limitador GLOBAL de pedidos/segundo, partilhado por TODAS as chamadas
# rpc_call() (analyzer, momentum/caveira, deployer, liquidez, copy...).
# Espaca os pedidos PREVENTIVAMENTE para nunca bater o limite do plano do
# RPC (Helius), em vez de so reagir ao 429 depois de ele acontecer - o
# retry com backoff acima continua como rede de seguranca, mas passa a
# ser a excecao, nao a regra (o diagnostico real mostrou 717 tokens do
# Caveira rejeitados por "dados on-chain indisponiveis" causados por 429
# persistente mesmo com retry). Taxa configuravel no .env
# (RPC_MAX_PEDIDOS_POR_SEGUNDO, default 8 - conservador para o plano
# gratuito da Helius, que aguenta ~10/s). Thread-safe: o websocket e o
# copy trading correm em threads proprias e partilham este mesmo balde.
_limitador_global = rate_limiter.RateLimiter(config.RPC_MAX_PEDIDOS_POR_SEGUNDO)


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
        # Espaco preventivo entre pedidos (tambem nas retentativas - um
        # retry e um pedido como outro qualquer para o limite do RPC)
        _limitador_global.adquirir()
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


# ==========================================================================
# Liquidez bloqueada/queimada (o sinal mais forte contra rug pulls)
# ==========================================================================
# Ideia: quem cria um pool na Raydium recebe "LP tokens" (o recibo da
# liquidez). Se esses LP tokens forem QUEIMADOS (enviados para o endereco
# incinerador) ou bloqueados, o criador NAO consegue retirar a liquidez
# -> rug pull impossivel por essa via. Se ficarem numa carteira normal,
# o criador pode sacar tudo a qualquer momento.

import base64 as _base64

# Programa Raydium AMM v4 (o mais comum nos pools classicos) e o tamanho
# fixo da conta de pool desse programa
_PROGRAMA_RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
_TAMANHO_POOL_V4 = 752
# No layout v4, o mint do LP token vive nos bytes 464..496 da conta
_OFFSET_LP_MINT_V4 = 464

# Endereco "incinerador" oficial da Solana: tokens enviados para aqui
# estao queimados para sempre (ninguem tem a chave privada)
_ENDERECO_INCINERADOR = "1nc1nerator11111111111111111111111111111111"

# DEXes onde a liquidez e gerida pelo PROTOCOLO (bonding curve do
# pump.fun; na migracao para pumpswap o LP e queimado automaticamente)
_DEX_PROTOCOLO = {"pump-fun", "pumpfun", "pump_fun", "pumpswap", "pump-swap"}


def _codificar_base58(dados: bytes) -> str:
    """Converte 32 bytes (uma pubkey) para o formato base58 da Solana.
    Implementado a mao para nao obrigar a instalar a biblioteca base58
    so por causa disto."""
    alfabeto = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    numero = int.from_bytes(dados, "big")
    texto = ""
    while numero > 0:
        numero, resto = divmod(numero, 58)
        texto = alfabeto[resto] + texto
    # Zeros a esquerda viram '1' em base58
    for byte in dados:
        if byte == 0:
            texto = "1" + texto
        else:
            break
    return texto


def verificar_liquidez_bloqueada(pool_address: str, dex: str) -> str:
    """Verifica se a liquidez do pool esta protegida contra rug pull.

    Devolve UMA de quatro respostas:
      "queimada"            -> LP queimado (incinerador/supply=0). MUITO seguro.
      "bloqueada_protocolo" -> dex pump.fun/pumpswap: o protocolo gere a
                               liquidez, o criador nao lhe toca. Seguro.
      "nao_bloqueada"       -> LP numa carteira normal com a maioria dos
                               tokens. O criador pode sacar tudo. RED FLAG.
      "desconhecido"        -> nao foi possivel determinar (outro DEX,
                               falha de RPC...). Nao penaliza nem beneficia.

    Nunca levanta excecao - qualquer falha devolve "desconhecido".
    """
    try:
        # --- Caso 1: DEX gerido pelo protocolo -> nao ha LP para roubar ---
        if (dex or "").lower() in _DEX_PROTOCOLO:
            return "bloqueada_protocolo"

        # --- Caso 2: Raydium v4 -> ler o mint do LP da conta do pool ---
        resultado = rpc_call(
            "getAccountInfo", [pool_address, {"encoding": "base64"}], tentativas=2
        )
        valor = resultado.get("value")
        if not valor:
            return "desconhecido"

        if valor.get("owner") != _PROGRAMA_RAYDIUM_V4:
            return "desconhecido"  # outro programa/layout - nao arriscamos ler mal

        dados_b64 = (valor.get("data") or ["", ""])[0]
        dados = _base64.b64decode(dados_b64)
        if len(dados) != _TAMANHO_POOL_V4:
            return "desconhecido"

        lp_mint = _codificar_base58(dados[_OFFSET_LP_MINT_V4:_OFFSET_LP_MINT_V4 + 32])

        # --- Supply atual do LP (se ~0, foi tudo queimado) ---
        info_lp = get_mint_info(lp_mint)
        if info_lp is None:
            return "desconhecido"
        if info_lp["supply"] <= 0:
            return "queimada"

        # --- Quem detem o LP? ---
        detentores = get_maiores_holders(lp_mint, supply=info_lp["supply"], limite=5)
        if not detentores:
            return "desconhecido"

        maior = detentores[0]
        # A conta incineradora como maior detentor = LP queimado
        if maior["endereco"] == _ENDERECO_INCINERADOR:
            return "queimada"
        # Maioria do LP numa unica conta normal = o dono pode sacar a liquidez
        if maior["pct"] > 50.0:
            return "nao_bloqueada"

        # LP espalhado por varias contas sem dominante claro - inconclusivo
        return "desconhecido"

    except Exception:
        return "desconhecido"


# ==========================================================================
# Historico do criador do token (deployer)
# ==========================================================================
def obter_deployer(mint: str) -> str | None:
    """Descobre a carteira que CRIOU o token (o "deployer").

    Como: a transacao mais ANTIGA que toca no mint e a da criacao, e o
    "fee payer" (primeira conta da transacao) e quem a pagou = o criador.
    Para tokens com minutos de vida, todas as assinaturas cabem numa
    pagina (limite 1000), por isso bastam 2 chamadas RPC.

    Devolve None se nao conseguir determinar (nunca levanta excecao).
    """
    try:
        assinaturas = rpc_call(
            "getSignaturesForAddress", [mint, {"limit": 1000}], tentativas=2
        )
        if not assinaturas or not isinstance(assinaturas, list):
            return None
        # A lista vem da mais RECENTE para a mais antiga -> a ultima e a criacao.
        # (Se o token ja tiver 1000+ transacoes, a "ultima" pode nao ser a
        # criacao - devolvemos None para nao apontar o dedo a carteira errada.)
        if len(assinaturas) >= 1000:
            return None
        mais_antiga = assinaturas[-1].get("signature")
        if not mais_antiga:
            return None

        tx = rpc_call("getTransaction", [
            mais_antiga,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ], tentativas=2)
        contas = (
            tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )
        if not contas:
            return None
        # O fee payer e sempre a primeira conta da mensagem
        primeira = contas[0]
        return primeira.get("pubkey") if isinstance(primeira, dict) else str(primeira)
    except Exception:
        return None


def contar_tokens_criados(deployer: str, janela_horas: int = 48) -> int | None:
    """Conta quantos tokens o deployer criou nas ultimas 'janela_horas'.

    Usa a API Enhanced da Helius (transacoes ja interpretadas, com um
    campo "type" legivel) - a api-key e extraida do SOLANA_RPC_URL do
    .env. Se o RPC configurado nao for Helius, devolve None (sinal
    "desconhecido", sem penalizar).

    Tipos que contam como criacao de token:
      - "CREATE"      -> lancamento no pump.fun
      - "TOKEN_MINT"  -> mint de token generico
    """
    try:
        # Extrair a api-key do URL (ex: https://mainnet.helius-rpc.com/?api-key=XYZ)
        if "helius" not in config.SOLANA_RPC_URL.lower():
            return None
        api_key = None
        if "api-key=" in config.SOLANA_RPC_URL:
            api_key = config.SOLANA_RPC_URL.split("api-key=")[1].split("&")[0]
        if not api_key:
            return None

        # A API Enhanced partilha o plano/creditos da Helius com o RPC -
        # passa pelo mesmo balde global para o total nunca exceder a taxa
        _limitador_global.adquirir()
        resposta = requests.get(
            f"https://api.helius.xyz/v0/addresses/{deployer}/transactions",
            params={"api-key": api_key, "limit": 100},
            timeout=15,
        )
        resposta.raise_for_status()
        transacoes = resposta.json()
        if not isinstance(transacoes, list):
            return None

        limite_tempo = time.time() - janela_horas * 3600
        criacoes = 0
        for tx in transacoes:
            if tx.get("timestamp", 0) < limite_tempo:
                continue  # fora da janela
            if tx.get("type") in ("CREATE", "TOKEN_MINT"):
                criacoes += 1
        return criacoes
    except Exception:
        return None


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
