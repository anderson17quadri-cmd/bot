/* ==========================================================================
   app.js - logica do dashboard no browser
   ==========================================================================
   O que este ficheiro faz, por ordem:
     1. Define funcoes de formatacao (dinheiro, datas, tempo decorrido)
     2. Cria os dois graficos Chart.js (vazios)
     3. A cada 8 segundos pede os dados ao Flask (/api/resumo e
        /api/posicoes) e atualiza cartoes, graficos e tabelas
   Nao ha nenhum framework - e JavaScript "puro", proposital para
   ser facil de ler e aprender.
   ========================================================================== */

// Cores - as mesmas variaveis do style.css, lidas do proprio CSS
// para nunca ficarem dessincronizadas entre graficos e resto da pagina
const css = getComputedStyle(document.documentElement);
const COR_AZUL = css.getPropertyValue("--azul").trim();
const COR_VERDE = css.getPropertyValue("--verde").trim();
const COR_VERMELHO = css.getPropertyValue("--vermelho").trim();
const COR_TEXTO_MUDO = css.getPropertyValue("--texto-mudo").trim();
const COR_GRELHA = css.getPropertyValue("--linha-grelha").trim();

const INTERVALO_POLLING_MS = 8000; // pede dados novos a cada 8 segundos

// ==========================================================================
// 1) Funcoes de formatacao
// ==========================================================================

/** Formata um numero como dinheiro: 12.3456 -> "$12.35" */
function dinheiro(valor) {
  if (valor === null || valor === undefined) return "N/A";
  return "$" + valor.toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Igual, mas com sinal explicito: +$1.20 / -$0.50 (para lucros) */
function dinheiroComSinal(valor) {
  if (valor === null || valor === undefined) return "N/A";
  return (valor >= 0 ? "+" : "-") + dinheiro(Math.abs(valor));
}

/** Formata uma percentagem com sinal: +1,15% / -3,20% */
function percentagemComSinal(valor) {
  const texto = Math.abs(valor).toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (valor >= 0 ? "+" : "-") + texto + "%";
}

/** Converte um timestamp ISO (UTC) para data/hora local legivel */
function dataHora(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleDateString("pt-PT", { day: "2-digit", month: "2-digit" }) +
         " " + d.toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
}

/** "Ha quanto tempo": recebe o ISO da compra e devolve ex. "2h 15m" */
function tempoDecorrido(iso) {
  if (!iso) return "–";
  const segundos = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const dias = Math.floor(segundos / 86400);
  const horas = Math.floor((segundos % 86400) / 3600);
  const minutos = Math.floor((segundos % 3600) / 60);
  if (dias > 0) return `${dias}d ${horas}h`;
  if (horas > 0) return `${horas}h ${minutos}m`;
  return `${minutos}m`;
}

/** Pinta um elemento de verde/vermelho consoante o valor ser +/- */
function pintarPorSinal(elemento, valor) {
  elemento.classList.remove("positivo", "negativo");
  if (valor > 0) elemento.classList.add("positivo");
  if (valor < 0) elemento.classList.add("negativo");
}

/** Atalho para document.getElementById - usado em todo o lado */
function el(id) { return document.getElementById(id); }

// ==========================================================================
// 2) Criacao dos graficos (comecam vazios, o polling enche-os)
// ==========================================================================

// Opcoes partilhadas: grelha discreta, texto mudo, sem animacao a cada
// refresh (senao os graficos "dancavam" de 8 em 8 segundos)
const opcoesBase = {
  responsive: true,
  maintainAspectRatio: false, // deixa o CSS (.area-grafico) mandar na altura
  animation: false,
  plugins: { legend: { display: false } }, // uma serie so -> o titulo do painel chega
  scales: {
    x: {
      ticks: { color: COR_TEXTO_MUDO, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
      grid: { display: false },
    },
    y: {
      ticks: { color: COR_TEXTO_MUDO, callback: (v) => "$" + v },
      grid: { color: COR_GRELHA },
    },
  },
};

// --- Grafico 1: curva de saldo (linha) -----------------------------------
const graficoSaldo = new Chart(el("grafico-saldo"), {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      label: "Saldo (USD)",
      data: [],
      borderColor: COR_AZUL,
      backgroundColor: COR_AZUL,
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 5, // alvo de toque maior no telemovel
      tension: 0.15,       // curva ligeiramente suavizada
    }],
  },
  options: opcoesBase,
});

// --- Grafico 2: "velas" de lucro/prejuizo por venda -----------------------
// Cada venda fechada e uma barra que parte de 0: para cima e verde
// (lucro), para baixo e vermelha (prejuizo). Os dados chegam como
// pares [0, lucro] ("floating bars" do Chart.js).
const graficoVelas = new Chart(el("grafico-velas"), {
  type: "bar",
  data: {
    labels: [],
    datasets: [{
      label: "Lucro da venda (USD)",
      data: [],
      backgroundColor: [], // uma cor por barra, definida no refresh
      borderRadius: 4,     // pontas arredondadas
      borderSkipped: false,
      maxBarThickness: 26,
    }],
  },
  options: {
    ...opcoesBase,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          // No tooltip mostra o lucro real, nao o par [0, lucro]
          label: (ctx) => " " + dinheiroComSinal(ctx.raw[1]),
        },
      },
    },
  },
});

// ==========================================================================
// 3) Polling: buscar dados ao Flask e atualizar a pagina
// ==========================================================================

/** Atualiza cartoes, badge, graficos e tabela de historico */
async function atualizarResumo() {
  const resposta = await fetch("/api/resumo");
  const dados = await resposta.json();
  const c = dados.cartoes;

  // --- Badge do modo (a parte mais importante da pagina!) ---
  const badge = el("badge-modo");
  if (dados.dry_run) {
    badge.textContent = "DRY RUN · dinheiro simulado";
    badge.className = "badge dry-run";
  } else {
    badge.textContent = "MODO REAL · dinheiro verdadeiro";
    badge.className = "badge real";
  }

  // --- Cartoes de resumo ---
  el("c-saldo-inicial").textContent = dinheiro(c.saldo_inicial);
  el("c-saldo-livre").textContent = dinheiro(c.saldo_livre);
  el("c-valor-posicoes").textContent = dinheiro(c.valor_posicoes);
  el("c-valor-total").textContent = dinheiro(c.valor_total);
  el("c-n-compras").textContent = c.n_compras;
  el("c-n-vendas").textContent = c.n_vendas;
  el("c-win-rate").textContent = c.win_rate === null ? "–" : c.win_rate + "%";

  const lucro = el("c-lucro-total");
  lucro.textContent = `${dinheiroComSinal(c.lucro_total)} (${percentagemComSinal(c.lucro_total_pct)})`;
  pintarPorSinal(lucro, c.lucro_total);

  // --- Grafico da curva de saldo ---
  const curva = dados.curva_saldo;
  const temTrades = curva.length > 1; // o 1.o ponto e so o saldo inicial
  el("vazio-saldo").hidden = temTrades;
  graficoSaldo.data.labels = curva.map((p) => (p.timestamp ? dataHora(p.timestamp) : "início"));
  graficoSaldo.data.datasets[0].data = curva.map((p) => p.saldo);
  graficoSaldo.update();

  // --- Grafico de velas (ganhos vs perdas por venda) ---
  const gp = dados.ganhos_perdas;
  el("gp-ganho").textContent = dinheiro(gp.total_ganho);
  el("gp-perdido").textContent = dinheiro(gp.total_perdido);
  el("vazio-velas").hidden = gp.velas.length > 0;
  graficoVelas.data.labels = gp.velas.map((v) => v.simbolo);
  graficoVelas.data.datasets[0].data = gp.velas.map((v) => [0, v.lucro_usd]); // barra de 0 ate ao lucro
  graficoVelas.data.datasets[0].backgroundColor = gp.velas.map(
    (v) => (v.lucro_usd >= 0 ? COR_VERDE : COR_VERMELHO)
  );
  graficoVelas.update();

  // --- Tabela de historico (mais recente primeiro, ja vem ordenada) ---
  const corpo = el("tabela-historico");
  el("vazio-historico").hidden = dados.historico.length > 0;
  corpo.innerHTML = dados.historico.map((h) => {
    const eVenda = h.tipo === "venda";
    // So as vendas tem lucro; nas compras a celula fica vazia
    let celulaLucro = "<td>–</td>";
    if (eVenda && h.lucro_usd !== undefined) {
      const classe = h.lucro_usd >= 0 ? "positivo" : "negativo";
      celulaLucro = `<td class="${classe}">${dinheiroComSinal(h.lucro_usd)}</td>`;
    }
    return `<tr>
      <td>${dataHora(h.timestamp)}</td>
      <td><span class="tag ${eVenda ? "venda" : "compra"}">${eVenda ? "VENDA" : "COMPRA"}</span></td>
      <td>${h.simbolo || "?"}</td>
      <td>${dinheiro(h.valor_usd)}</td>
      ${celulaLucro}
    </tr>`;
  }).join("");
}

/** Atualiza a tabela de posicoes abertas (com preco atual da Jupiter) */
async function atualizarPosicoes() {
  const resposta = await fetch("/api/posicoes");
  const dados = await resposta.json();
  const posicoes = dados.posicoes;

  el("vazio-posicoes").hidden = posicoes.length > 0;
  el("tabela-posicoes").innerHTML = posicoes.map((p) => {
    // P/L nao realizado: pode ser null se a cotacao Jupiter falhou
    let celulaPL = "<td>N/A</td>";
    if (p.lucro_nao_realizado_usd !== null) {
      const classe = p.lucro_nao_realizado_usd >= 0 ? "positivo" : "negativo";
      celulaPL = `<td class="${classe}">${dinheiroComSinal(p.lucro_nao_realizado_usd)}</td>`;
    }
    return `<tr>
      <td>${p.simbolo}</td>
      <td>${dinheiro(p.valor_investido_usd)}</td>
      <td>$${p.preco_compra_usd.toPrecision(4)}</td>
      <td>${p.quantidade_tokens.toLocaleString("pt-PT", { maximumFractionDigits: 0 })}</td>
      <td>${tempoDecorrido(p.timestamp_compra)}</td>
      <td>${p.valor_atual_usd === null ? "N/A" : dinheiro(p.valor_atual_usd)}</td>
      ${celulaPL}
    </tr>`;
  }).join("");
}

/** Um ciclo completo de atualizacao. try/catch para que uma falha de
    rede momentanea nao mate o polling - tenta outra vez no proximo ciclo */
async function atualizarTudo() {
  try {
    // As duas chamadas em paralelo (a de posicoes pode demorar por
    // causa das cotacoes Jupiter; assim nao atrasa os cartoes)
    await Promise.all([atualizarResumo(), atualizarPosicoes()]);
    el("ultima-atualizacao").textContent =
      "Atualizado às " + new Date().toLocaleTimeString("pt-PT");
  } catch (erro) {
    el("ultima-atualizacao").textContent =
      "Sem ligação ao servidor... a tentar de novo";
  }
}

// Arranque: atualiza ja, e depois repete a cada 8 segundos
atualizarTudo();
setInterval(atualizarTudo, INTERVALO_POLLING_MS);
