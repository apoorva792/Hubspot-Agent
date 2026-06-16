const EXAMPLES = [
  "Warm leads being actively worked (status Working or Demo Booked)",
  "US-based MQLs we haven't contacted in the last 30 days",
  "Decision-makers (Head/VP/Chief) at banks in the Middle East",
  "SQLs created in the last 14 days, newest first",
];

const $ = (id) => document.getElementById(id);

function renderExamples() {
  const box = $("examples");
  EXAMPLES.forEach((text) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = text;
    chip.onclick = () => {
      $("prompt").value = text;
      $("prompt").focus();
    };
    box.appendChild(chip);
  });
}

function setStatus(msg, isError = false) {
  const el = $("status");
  el.classList.remove("hidden");
  el.classList.toggle("error", isError);
  el.innerHTML = isError
    ? msg
    : `<span class="spinner"></span><span>${msg}</span>`;
}

function clearStatus() {
  $("status").classList.add("hidden");
}

function isUrl(v) {
  return typeof v === "string" && v.startsWith("http");
}

function renderTable(columns, rows) {
  const table = $("table");
  table.innerHTML = "";

  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  columns.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((c) => {
      const td = document.createElement("td");
      const val = row[c] ?? "";
      if (isUrl(val)) {
        const a = document.createElement("a");
        a.href = val;
        a.target = "_blank";
        a.textContent = "open ↗";
        td.appendChild(a);
      } else {
        td.textContent = val;
        td.title = val;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

async function run() {
  const prompt = $("prompt").value.trim();
  if (!prompt) return;

  $("run").disabled = true;
  $("results").classList.add("hidden");
  setStatus("Discovering schema, translating your request, and searching HubSpot…");

  try {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const data = await res.json();

    if (!res.ok) {
      setStatus(data.detail || "Something went wrong.", true);
      return;
    }

    clearStatus();
    $("results").classList.remove("hidden");

    const total = data.total ?? 0;
    const count = data.count ?? 0;
    $("count-line").textContent =
      count === 0
        ? "No matching leads found"
        : `${total.toLocaleString()} leads matched · ${count.toLocaleString()} fetched`;
    $("summary").textContent = data.summary || "";
    $("query").textContent = JSON.stringify(data.query, null, 2);

    if (data.csv_id) {
      const dl = $("download");
      dl.href = `/api/download/${data.csv_id}`;
      dl.classList.remove("hidden");
    } else {
      $("download").classList.add("hidden");
    }

    renderTable(data.columns || [], data.rows || []);
    $("preview-note").textContent =
      count > (data.rows?.length || 0)
        ? `Showing first ${data.rows.length} of ${count} fetched. Full set is in the CSV.`
        : "";
  } catch (e) {
    setStatus(`Request failed: ${e.message}`, true);
  } finally {
    $("run").disabled = false;
  }
}

renderExamples();
$("run").onclick = run;
$("prompt").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run();
});
