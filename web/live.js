(() => {
  const api = "";
  const banner = document.querySelector(".mock-banner");
  if (banner) {
    banner.innerHTML = "<strong>LIVE</strong> API 接続済み。score は類似度であり確率ではありません。";
  }

  function toast(title, message) {
    if (window.DB2RAGMock?.showToast) {
      window.DB2RAGMock.showToast(title, message);
      return;
    }
    window.alert(`${title}: ${message}`);
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function errorDetail(data, fallback) {
    const detail = data && data.detail;
    if (detail == null) return data?.error || fallback;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => item?.msg || item?.message || JSON.stringify(item)).join("; ");
    }
    if (typeof detail === "object") return detail.msg || detail.message || JSON.stringify(detail);
    return String(detail);
  }

  async function apiJson(method, path, payload) {
    const options = { method, headers: { "Accept": "application/json" } };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(api + path, options);
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!response.ok) throw new Error(errorDetail(data, text || String(response.status)));
    return data;
  }

  function page() {
    const path = window.location.pathname;
    if (path === "/" || path.endsWith("/index.html")) return "list";
    if (path.includes("wizard")) return "wizard";
    if (path.includes("progress")) return "progress";
    if (path.includes("jobs")) return "jobs";
    if (path.includes("search")) return "search";
    return "";
  }

  async function showVersion() {
    const nodes = document.querySelectorAll("[data-app-version]");
    if (!nodes.length) return;
    try {
      const data = await apiJson("GET", "/api/v1/version");
      nodes.forEach((node) => { node.textContent = `v${data.version}`; });
      window.DB2RAGVersion = data;
      if (data.reranker_enabled) {
        const rerankerSwitch = document.getElementById("reranker-switch");
        rerankerSwitch?.classList.remove("is-disabled");
        rerankerSwitch?.removeAttribute("aria-disabled");
        const note = "設定が有効です。ONで再順位付けします。";
        const noteNode = document.getElementById("reranker-note");
        const diag = document.getElementById("reranker-diag");
        if (noteNode) noteNode.textContent = note;
        if (diag) diag.textContent = note;
      }
    } catch (error) {
      nodes.forEach((node) => { node.textContent = "v—"; });
    }
  }

  showVersion();

  document.querySelectorAll("a.nav-link").forEach((link) => {
    const href = link.getAttribute("href") || "";
    const text = link.textContent || "";
    if (href.includes("progress") || /進捗|進歩/.test(text)) {
      link.remove();
    }
  });

  async function loadRags() {
    const data = await apiJson("GET", "/api/v1/rags");
    return data.rags || [];
  }

  function wireListRefresh() {
    const refresh = document.getElementById("rags-refresh");
    if (!refresh || refresh.dataset.bound === "1") return;
    refresh.dataset.bound = "1";
    refresh.removeAttribute("data-toast");
    refresh.removeAttribute("data-toast-detail");
    refresh.addEventListener("click", () => {
      bindList().catch((error) => toast("一覧取得失敗", String(error)));
    });
  }

  async function bindList() {
    wireListRefresh();
    const [rags, health, jobsData] = await Promise.all([
      loadRags(),
      apiJson("GET", "/api/v1/health").catch(() => null),
      apiJson("GET", "/api/v1/jobs").catch(() => ({ jobs: [] })),
    ]);
    const jobs = jobsData.jobs || [];
    const tbody = document.querySelector("table tbody");
    if (tbody) {
      tbody.innerHTML = rags.length ? rags.map((rag) => {
        const running = Number(rag.running_jobs || 0) > 0;
        const source = running ? `変換中 ${rag.running_jobs}` : (rag.acl_tag_default || "—");
        return `
        <tr>
          <td><strong>${escapeHtml(rag.display_name)}</strong><br><span class="mono">${escapeHtml(rag.rag_id)}</span></td>
          <td><span class="status-pill ${rag.status === "active" ? "success" : "warning"}">${escapeHtml(rag.status || "")}</span></td>
          <td>${escapeHtml(source)}</td>
          <td class="mono">${rag.child_count ?? 0}</td>
          <td>${escapeHtml(rag.updated_at || "")}</td>
          <td><div class="inline-actions">
            <a class="button small secondary" href="/search?rag=${encodeURIComponent(rag.rag_id)}">検索</a>
            <button class="button small secondary" type="button" data-rag-dify="${escapeHtml(rag.rag_id)}">Dify連携</button>
            <button class="button small secondary" type="button" data-rag-export="${escapeHtml(rag.rag_id)}">出力</button>
            ${running ? `<button class="button small danger" type="button" data-rag-stop="${escapeHtml(rag.rag_id)}">停止</button>` : ""}
            <button class="button small warning" type="button" data-rag-delete="${escapeHtml(rag.rag_id)}" data-rag-name="${escapeHtml(rag.display_name || rag.rag_id)}" data-rag-running="${running ? "1" : "0"}">削除</button>
          </div></td>
        </tr>`;
      }).join("") : `<tr><td colspan="6">RAGはまだありません。新規作成から投入してください。</td></tr>`;
      tbody.querySelectorAll("[data-rag-dify]").forEach((button) => {
        button.addEventListener("click", async () => {
          const dialog = document.getElementById("dify-dialog");
          if (!dialog || typeof dialog.showModal !== "function") return;
          try {
            const config = await apiJson("GET", `/api/v1/rags/${encodeURIComponent(button.dataset.ragDify)}/dify-config`);
            const fill = (inputId, copyId, value) => {
              const input = document.getElementById(inputId);
              const copy = document.getElementById(copyId);
              if (input) input.value = value || "";
              if (copy) copy.dataset.copy = value || "";
            };
            fill("dify-endpoint-docker", "dify-copy-docker", config.endpoint_docker);
            fill("dify-endpoint-local", "dify-copy-local", config.endpoint_local);
            fill("dify-knowledge-id", "dify-copy-id", config.knowledge_id);
            const note = document.getElementById("dify-auth-note");
            if (note) {
              note.textContent = config.auth_required
                ? `API キーが必要です。既定 ACL は ${config.default_acl} です。`
                : `このPCからの疎通に API キーは不要です。既定 ACL は ${config.default_acl} です。`;
            }
            dialog.showModal();
          } catch (error) {
            toast("Dify連携情報を取得できません", String(error));
          }
        });
      });
      tbody.querySelectorAll("[data-rag-export]").forEach((button) => {
        button.addEventListener("click", async () => {
          if (button.disabled) return;
          button.disabled = true;
          try {
            const result = await apiJson("POST", "/api/v1/export", { rag_id: button.dataset.ragExport });
            toast("エクスポート完了", result.path || result.export_id || "");
          } catch (error) {
            toast("エクスポート失敗", String(error));
          } finally {
            button.disabled = false;
          }
        });
      });
      tbody.querySelectorAll("[data-rag-delete]").forEach((button) => {
        button.addEventListener("click", async () => {
          const ragId = button.dataset.ragDelete;
          const name = button.dataset.ragName || ragId;
          const runningHint = button.dataset.ragRunning === "1" ? "実行中のジョブも停止します。" : "";
          if (!window.confirm(`「${name}」を削除します。ベクトル・索引・エクスポートは復元できません。${runningHint}`)) {
            return;
          }
          button.disabled = true;
          try {
            await apiJson("DELETE", `/api/v1/rags/${encodeURIComponent(ragId)}`);
            toast("削除しました", name);
            await bindList();
          } catch (error) {
            toast("削除に失敗", String(error));
            button.disabled = false;
          }
        });
      });
      tbody.querySelectorAll("[data-rag-stop]").forEach((button) => {
        button.addEventListener("click", async () => {
          const ragId = button.dataset.ragStop;
          const targets = jobs.filter((job) => job.rag_id === ragId && ["pending", "running", "verifying"].includes(job.status));
          if (!targets.length) {
            toast("停止するジョブがありません", ragId);
            return;
          }
          try {
            await Promise.all(targets.map((job) => apiJson("POST", `/api/v1/jobs/${job.job_id}/cancel`)));
            toast("停止しました", `${targets.length} ジョブ`);
            await bindList();
          } catch (error) {
            toast("停止に失敗", String(error));
          }
        });
      });
    }
    const active = rags.filter((r) => r.status === "active").length;
    const chunks = rags.reduce((sum, r) => sum + Number(r.child_count || 0), 0);
    const running = rags.reduce((sum, r) => sum + Number(r.running_jobs || 0), 0);
    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    };
    setText("metric-active", String(active).padStart(2, "0"));
    setText("metric-chunks", String(chunks));
    setText("metric-storage", "—");
    setText("metric-jobs", String(running).padStart(2, "0"));
    setText("metric-storage-foot", "容量APIなし");
    setText("metric-jobs-foot", running ? "実行中ジョブあり" : "実行中なし");
    const qdrant = health?.qdrant || {};
    const gpu = health?.gpu || {};
    const sys = document.getElementById("system-status");
    if (sys) {
      const qOk = Boolean(qdrant.ok);
      const pill = sys.querySelector(".status-pill");
      if (pill) {
        pill.className = `status-pill ${health?.ok ? "success" : "warning"}`;
        pill.textContent = health?.ok ? "正常" : "確認";
      }
      setText("sys-qdrant", qOk ? (qdrant.version || "OK") : "DOWN");
      setText("sys-qdrant-foot", qOk ? "接続中" : "未接続");
      setText("sys-gpu", gpu.cuda ? (gpu.gpu_name || "CUDA") : "CPU");
      setText("sys-gpu-foot", gpu.cuda ? "CUDA" : "FP32 · GPUなし");
      setText("sys-sqlite", "WAL");
      setText("sys-sqlite-foot", health?.time || "—");
    }
    const activity = document.getElementById("activity-list");
    if (activity) {
      const recent = jobs.slice(0, 4);
      activity.innerHTML = recent.length ? recent.map((job) => `
        <div class="event-row">
          <span class="event-icon">${itemIcon(job.status)}</span>
          <div class="event-main"><strong>${escapeHtml(job.display_name || job.rag_id || job.job_id)}</strong><span>${escapeHtml(job.status)} · ${Number(job.progress_pct || 0).toFixed(1)}%</span></div>
          <span class="mono muted">${escapeHtml((job.updated_at || job.created_at || "").slice(11, 16) || "")}</span>
        </div>`).join("") : `<div class="callout">まだジョブはありません。</div>`;
    }
  }

  function statusClass(status) {
    if (status === "completed") return "success";
    if (status === "failed" || status === "cancelled") return "danger";
    if (status === "pending") return "neutral";
    return "running";
  }

  function formatJobDateTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (!Number.isFinite(date.getTime())) return "—";
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function formatJobElapsed(startIso, endIso) {
    const started = Date.parse(startIso || "");
    if (!Number.isFinite(started)) return "—";
    const ended = endIso ? Date.parse(endIso) : Date.now();
    if (!Number.isFinite(ended)) return "—";
    const sec = Math.max(0, Math.floor((ended - started) / 1000));
    const hours = Math.floor(sec / 3600);
    const minutes = Math.floor((sec % 3600) / 60);
    const seconds = sec % 60;
    if (hours) return `${hours}時間${minutes}分${seconds}秒`;
    if (minutes) return `${minutes}分${seconds}秒`;
    return `${seconds}秒`;
  }

  function jobWorkLabel(job) {
    const append = job.mode === "append";
    return {
      tag: append ? "追加" : "新規",
      tagClass: append ? "warning" : "success",
    };
  }

  function jobPhase(job) {
    if (job.status === "completed") return { text: "完了", cls: "success" };
    if (job.status === "failed") return { text: "失敗", cls: "danger" };
    if (job.status === "cancelled") return { text: "取消", cls: "danger" };
    const pct = Number(job.progress_pct || 0);
    if (job.status === "pending" || job.status === "paused" || pct <= 0) {
      return { text: "モデル起動中", cls: "neutral" };
    }
    return { text: "RAG変換中", cls: "running" };
  }

  function itemIcon(status) {
    if (status === "completed") return "✓";
    if (status === "failed") return "!";
    if (status === "running") return "◔";
    return "·";
  }

  let jobHoverTimer = null;

  function stopJobHoverPoll() {
    if (jobHoverTimer) {
      clearInterval(jobHoverTimer);
      jobHoverTimer = null;
    }
  }

  function fillJobHover(detail) {
    const idLabel = document.getElementById("job-hover-id");
    const body = document.getElementById("job-hover-body");
    if (idLabel) idLabel.textContent = detail.job_id || "";
    if (!body) return;
    const items = detail.items || [];
    const failed = items.filter((item) => item.status === "failed" || item.error_message);
    const pct = Number(detail.progress_pct || 0);
    const pill = statusClass(detail.status);
    const itemRows = items.length ? items.map((item) => {
      const name = item.object_key || item.object_id || item.job_item_id;
      const err = item.error_message || item.error_code || "";
      return `
        <div class="error-row">
          <span class="file-icon">${itemIcon(item.status)}</span>
          <div class="file-main">
            <strong>${escapeHtml(name)}</strong>
            <span>${escapeHtml(item.status || "")} · checkpoint ${escapeHtml(item.checkpoint || "—")}${err ? ` · ${escapeHtml(err)}` : ""}</span>
          </div>
          <span class="status-pill ${statusClass(item.status)}">${escapeHtml(item.status || "")}</span>
        </div>`;
    }).join("") : `<div class="callout">itemはまだありません。</div>`;
    const errorRows = failed.length ? failed.map((item) => `
        <div class="error-row">
          <span class="file-icon" style="background: var(--red-soft); color: var(--red);">!</span>
          <div class="file-main">
            <strong>${escapeHtml(item.object_key || item.object_id || "")} · ${escapeHtml(item.error_code || "ERROR")}</strong>
            <span>${escapeHtml(item.error_message || "メッセージなし")}</span>
          </div>
        </div>`).join("") : `<div class="callout">エラーはありません。</div>`;
    const jobErr = detail.error_message
      ? `<div class="callout warning">${escapeHtml(detail.error_code || "JOB")} · ${escapeHtml(detail.error_message)}</div>`
      : "";
    body.innerHTML = `
      ${jobErr}
      <div class="progress-header">
        <span class="status-pill ${pill}">${escapeHtml(detail.status || "")}</span>
        <div class="progress-percent">${pct.toFixed(1)}%</div>
      </div>
      <div class="progress-track" style="height: 10px;"><div class="progress-bar" style="width: ${pct}%;"></div></div>
      <div class="progress-stats">
        <div class="progress-stat"><span>RAG</span><strong>${escapeHtml(detail.display_name || detail.rag_id || "—")}</strong></div>
        <div class="progress-stat"><span>item</span><strong>${items.filter((i) => i.status === "completed").length} / ${items.length}</strong></div>
        <div class="progress-stat"><span>現在</span><strong>${escapeHtml(detail.current_item_name || "—")}</strong></div>
        <div class="progress-stat"><span>ETA</span><strong>${detail.eta_seconds ?? "—"}</strong></div>
      </div>
      <div class="job-hover-section-title">item 進捗</div>
      ${itemRows}
      <div class="job-hover-section-title">エラー</div>
      ${errorRows}`;
  }

  async function openJobDetail(jobId, anchor) {
    const detail = await apiJson("GET", `/api/v1/jobs/${jobId}`);
    fillJobHover(detail);
    window.DB2RAGMock?.placeJobHover?.(anchor);
    stopJobHoverPoll();
    if (detail.status === "running" || detail.status === "pending") {
      jobHoverTimer = setInterval(() => {
        apiJson("GET", `/api/v1/jobs/${jobId}`).then((next) => {
          fillJobHover(next);
          window.DB2RAGMock?.placeJobHover?.(anchor);
        }).catch(() => stopJobHoverPoll());
      }, 2000);
    }
  }

  let jobsPollTimer = null;
  let jobsRefreshBound = false;
  let jobsTableSig = null;
  let jobsFilterBound = false;
  let jobsStatusFilter = "all";
  let jobsLast24h = false;

  function applyJobFilters(jobs) {
    let out = jobs;
    if (jobsStatusFilter && jobsStatusFilter !== "all") {
      out = out.filter((job) => job.status === jobsStatusFilter);
    }
    if (jobsLast24h) {
      const cutoff = Date.now() - 24 * 3600 * 1000;
      out = out.filter((job) => {
        const stamp = Date.parse(job.created_at || job.started_at || "");
        return Number.isFinite(stamp) ? stamp >= cutoff : true;
      });
    }
    return out;
  }

  function wireJobFilters() {
    if (jobsFilterBound) return;
    const bar = document.getElementById("job-filters");
    if (!bar) return;
    jobsFilterBound = true;
    bar.querySelectorAll("[data-job-status]").forEach((chip) => {
      chip.addEventListener("click", () => {
        jobsStatusFilter = chip.dataset.jobStatus || "all";
        bar.querySelectorAll("[data-job-status]").forEach((peer) => {
          peer.classList.toggle("is-active", peer === chip);
        });
        jobsTableSig = null;
        bindJobs().catch((error) => toast("ジョブ取得失敗", String(error)));
      });
    });
    const windowChip = document.getElementById("job-window-24h");
    windowChip?.addEventListener("click", () => {
      jobsLast24h = !jobsLast24h;
      windowChip.classList.toggle("is-active", jobsLast24h);
      jobsTableSig = null;
      bindJobs().catch((error) => toast("ジョブ取得失敗", String(error)));
    });
  }

  function wireJobsRefresh() {
    if (jobsRefreshBound) return;
    const refresh = document.getElementById("jobs-refresh")
      || document.querySelector(".heading-actions .button.secondary");
    if (!refresh) return;
    jobsRefreshBound = true;
    refresh.removeAttribute("data-toast");
    refresh.removeAttribute("data-toast-detail");
    refresh.addEventListener("click", () => {
      bindJobs().catch((error) => toast("ジョブ取得失敗", String(error)));
    });
  }

  function renderJobQueue(jobs) {
    const body = document.getElementById("job-queue-body");
    const pill = document.getElementById("job-queue-pill");
    if (!body) return;
    const active = jobs.filter((job) => job.status === "running" || job.status === "pending");
    if (pill) {
      pill.className = `status-pill ${active.some((job) => job.status === "running") ? "running" : "neutral"}`;
      pill.textContent = active.length ? `${String(active.length).padStart(2, "0")} ACTIVE` : "IDLE";
    }
    if (!active.length) {
      body.innerHTML = "<div class=\"callout\">実行中のジョブはありません。モデルはロード済みです。新規変換から投入してください。</div>";
      return;
    }
    body.innerHTML = active.map((job, index) => `
      <div class="event-row">
        <span class="event-icon">${index + 1}</span>
        <div class="event-main">
          <strong>${escapeHtml(job.display_name || job.rag_id || job.job_id)}</strong>
          <span>${escapeHtml(job.status)} · ${Number(job.progress_pct || 0).toFixed(1)}% · ${escapeHtml(job.current_item_name || "—")}</span>
        </div>
        <span class="mono muted">${escapeHtml(job.status === "running" ? "現在" : "待機")}</span>
      </div>`).join("");
  }

  function renderWorker(health, runningCount) {
    const pill = document.getElementById("worker-pill");
    const body = document.getElementById("worker-body");
    const gpu = health?.gpu || {};
    const qdrant = health?.qdrant || {};
    const device = gpu.cuda ? (gpu.gpu_name || "CUDA") : "CPU FP32";
    const env = document.querySelector(".environment-pill");
    if (env) {
      env.innerHTML = `<span class="live-dot"></span>${escapeHtml(device)} / ${runningCount ? "BUSY" : "IDLE"}`;
    }
    if (pill) {
      const ok = Boolean(health?.ok) && (health?.status === "ok" || health?.status === "degraded");
      pill.className = `status-pill ${health?.status === "ok" ? "success" : "warning"}`;
      pill.textContent = ok ? String(health.status || "unknown").toUpperCase() : "DOWN";
    }
    if (!body) return;
    const warnings = (health?.warnings || []).map((warning) => `
      <div class="diagnostic"><span class="diagnostic-icon">!</span><div><strong>WARN</strong><span>${escapeHtml(warning)}</span></div></div>`).join("");
    body.innerHTML = `
      <div class="diagnostic"><span class="diagnostic-icon">${gpu.cuda ? "✓" : "!"}</span><div><strong>embedder</strong><span>${escapeHtml(device)} · ${runningCount ? "embedding" : "idle"}</span></div></div>
      <div class="diagnostic"><span class="diagnostic-icon">${qdrant.ok ? "✓" : "!"}</span><div><strong>qdrant</strong><span class="mono">${escapeHtml(qdrant.version || "—")}</span></div></div>
      <div class="diagnostic"><span class="diagnostic-icon">✓</span><div><strong>heartbeat</strong><span>${escapeHtml(health?.time || "—")}</span></div></div>
      ${warnings}`;
  }

  function fillJobMetrics(jobs, health) {
    const runningCount = jobs.filter((job) => job.status === "running").length;
    const queued = jobs.filter((job) => job.status === "pending").length;
    const completed = jobs.filter((job) => job.status === "completed").length;
    const metrics = document.querySelectorAll(".metric-value");
    const feet = document.querySelectorAll(".metric-foot");
    if (metrics[0]) metrics[0].textContent = String(runningCount).padStart(2, "0");
    if (metrics[1]) metrics[1].textContent = String(queued).padStart(2, "0");
    if (metrics[2]) metrics[2].textContent = String(completed).padStart(2, "0");
    if (metrics[3]) metrics[3].textContent = health?.gpu?.cuda ? "CUDA" : "CPU";
    if (feet[0]) feet[0].textContent = runningCount ? "workerが処理中" : "実行中なし";
    if (feet[1]) feet[1].textContent = queued ? "lease待ち" : "キュー空";
    if (feet[2]) feet[2].textContent = "完了ジョブ総数";
    if (feet[3]) feet[3].textContent = health?.gpu?.cuda ? (health.gpu.gpu_name || "CUDA") : "FP32 · GPUなし";
    renderJobQueue(jobs);
    renderWorker(health, runningCount);
    return runningCount;
  }

  async function bindJobs(options = {}) {
    wireJobsRefresh();
    wireJobFilters();
    const skipTable = Boolean(options.skipTable);
    const [data, health] = await Promise.all([
      apiJson("GET", "/api/v1/jobs"),
      apiJson("GET", "/api/v1/health").catch(() => null),
    ]);
    const jobs = data.jobs || [];
    fillJobMetrics(jobs, health);
    const visible = applyJobFilters(jobs);
    const hoverOpen = document.getElementById("job-detail-overlay")?.classList.contains("is-open");
    const tbody = document.querySelector("table tbody");
    const tableSig = `${jobsStatusFilter}|${jobsLast24h}|` + visible.map((job) => {
      const startIso = job.started_at || job.created_at || "";
      const elapsed = job.finished_at
        ? "done"
        : String(Math.floor((Date.now() - (Date.parse(startIso) || 0)) / 1000));
      return `${job.job_id}:${job.status}:${Number(job.progress_pct || 0)}:${job.current_item_name || ""}:${elapsed}`;
    }).join("|");
    if (!tbody || skipTable || hoverOpen || tableSig === jobsTableSig) {
      if (!jobsPollTimer) {
        jobsPollTimer = setInterval(() => {
          const open = document.getElementById("job-detail-overlay")?.classList.contains("is-open");
          bindJobs({ skipTable: open }).catch(() => {});
        }, 2000);
      }
      return;
    }
    jobsTableSig = tableSig;
    tbody.innerHTML = visible.length ? visible.map((job) => {
      const pct = Number(job.progress_pct || 0);
      const done = Number(job.work_units_done || 0);
      const total = Number(job.work_units_total || 0);
      const running = job.status === "running" || job.status === "pending";
      const failed = job.status === "failed";
      const detailLabel = failed ? "エラー" : "詳細";
      const detailClass = failed ? "button small warning" : "button small";
      const extra = running
        ? `<button class="button small danger" type="button" data-job-cancel="${job.job_id}">取消</button>`
        : (job.status === "cancelled" || job.status === "failed")
          ? `<button class="button small secondary" type="button" data-job-resume="${job.job_id}">再開</button>`
          : job.status === "completed"
            ? `<a class="button small secondary" href="/search?rag=${encodeURIComponent(job.rag_id || "")}">検索</a>`
            : "";
      const work = jobWorkLabel(job);
      const phase = jobPhase(job);
      const startIso = job.started_at || job.created_at || "";
      const startText = formatJobDateTime(startIso);
      const endText = job.finished_at ? formatJobDateTime(job.finished_at) : "—";
      const elapsedText = formatJobElapsed(startIso, job.finished_at);
      return `
      <tr>
        <td>
          <div class="job-work">
            <span class="tag ${work.tagClass}">${work.tag}</span>
            <div>
              <span class="row-title">${escapeHtml(job.display_name || job.rag_id || "名称未設定")}</span>
              <span class="row-subtitle mono">${escapeHtml(job.job_id)}</span>
            </div>
          </div>
        </td>
        <td><span class="status-pill ${phase.cls}">${escapeHtml(phase.text)}</span></td>
        <td style="min-width: 170px;"><div class="progress-track"><div class="progress-bar" style="width: ${pct}%;"></div></div><span class="row-subtitle mono">${pct.toFixed(1)}%</span></td>
        <td>
          <span class="row-title mono">${escapeHtml(startText)}</span>
          <span class="row-subtitle mono">${escapeHtml(endText)}</span>
          <span class="row-subtitle mono">${escapeHtml(elapsedText)}</span>
        </td>
        <td class="mono">${done} / ${total}</td>
        <td><div class="inline-actions">
          <button class="${detailClass}" type="button" data-job-detail data-job-live="true" data-job-id="${escapeHtml(job.job_id)}">${detailLabel}</button>
          ${extra}
        </div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="6">${jobs.length ? "この条件のジョブはありません。" : "ジョブはありません。"}</td></tr>`;

    tbody.querySelectorAll("[data-job-live='true']").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const pop = document.getElementById("job-detail-overlay");
        const idLabel = document.getElementById("job-hover-id");
        if (pop?.classList.contains("is-open") && idLabel?.textContent === button.dataset.jobId) {
          window.DB2RAGMock?.closeJobHover?.();
          return;
        }
        try {
          await openJobDetail(button.dataset.jobId, button);
        } catch (error) {
          toast("詳細を開けません", String(error));
        }
      });
    });
    tbody.querySelectorAll("[data-job-cancel]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await apiJson("POST", `/api/v1/jobs/${button.dataset.jobCancel}/cancel`);
          toast("取消しました", button.dataset.jobCancel);
          await bindJobs();
        } catch (error) {
          toast("取消に失敗", String(error));
        }
      });
    });
    tbody.querySelectorAll("[data-job-resume]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await apiJson("POST", `/api/v1/jobs/${button.dataset.jobResume}/resume`);
          toast("再開しました", button.dataset.jobResume);
          await bindJobs();
        } catch (error) {
          toast("再開に失敗", String(error));
        }
      });
    });
    const originalClose = window.DB2RAGMock?.closeJobHover;
    if (originalClose && !window.DB2RAGMock._jobHoverPatched) {
      window.DB2RAGMock.closeJobHover = () => {
        stopJobHoverPoll();
        originalClose();
      };
      window.DB2RAGMock._jobHoverPatched = true;
    }
    if (!jobsPollTimer) {
      jobsPollTimer = setInterval(() => {
        const open = document.getElementById("job-detail-overlay")?.classList.contains("is-open");
        bindJobs({ skipTable: open }).catch(() => {});
      }, 2000);
    }
  }

  async function bindProgress() {
    const data = await apiJson("GET", "/api/v1/jobs");
    const job = (data.jobs || [])[0];
    if (!job) return;
    const detail = await apiJson("GET", `/api/v1/jobs/${job.job_id}`);
    const fill = document.querySelector(".progress-fill, .bar-fill");
    if (fill) fill.style.width = `${detail.progress_pct || 0}%`;
  }

  function switchOn(element) {
    return Boolean(element?.classList.contains("on"));
  }

  function bindToggleSwitch(element) {
    if (!element || element.dataset.bound === "1") return;
    element.dataset.bound = "1";
    element.setAttribute("role", "switch");
    element.tabIndex = 0;
    const sync = () => element.setAttribute("aria-checked", switchOn(element) ? "true" : "false");
    sync();
    const toggle = () => {
      if (element.classList.contains("is-disabled")) return;
      element.classList.toggle("on");
      sync();
    };
    element.addEventListener("click", toggle);
    element.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
  }

  function markUnimplemented(element, reason) {
    if (!element) return;
    element.removeAttribute("data-toast");
    element.removeAttribute("data-toast-detail");
    element.disabled = true;
    element.setAttribute("aria-disabled", "true");
    if (reason) element.title = reason;
  }

  async function bindSearch() {
    const rags = await loadRags();
    const select = document.getElementById("rag-select");
    if (select) {
      select.innerHTML = rags.map((rag) =>
        `<option value="${escapeHtml(rag.rag_id)}" data-acl="${escapeHtml(rag.acl_tag_default || "internal")}">${escapeHtml(rag.display_name)} · ${rag.child_count || 0} chunks</option>`
      ).join("") || `<option value="">RAGなし</option>`;
      const wanted = new URLSearchParams(window.location.search).get("rag");
      if (wanted && [...select.options].some((option) => option.value === wanted)) {
        select.value = wanted;
      }
    }
    const aclFilter = document.getElementById("acl-filter");
    const sourceFilter = document.getElementById("source-type");
    const hybridSwitch = document.getElementById("hybrid-switch");
    const parentSwitch = document.getElementById("parent-switch");
    const dedupSwitch = document.getElementById("dedup-switch");
    const rerankerSwitch = document.getElementById("reranker-switch");
    bindToggleSwitch(hybridSwitch);
    bindToggleSwitch(parentSwitch);
    bindToggleSwitch(dedupSwitch);
    bindToggleSwitch(rerankerSwitch);
    const version = window.DB2RAGVersion;
    if (version?.reranker_enabled && rerankerSwitch) {
      rerankerSwitch.classList.remove("is-disabled");
      rerankerSwitch.removeAttribute("aria-disabled");
      const note = "設定が有効です。ONで再順位付けします。";
      const noteNode = document.getElementById("reranker-note");
      const diag = document.getElementById("reranker-diag");
      if (noteNode) noteNode.textContent = note;
      if (diag) diag.textContent = note;
    }
    markUnimplemented(document.getElementById("eval-save"), "評価セット保存は未実装です");
    markUnimplemented(document.getElementById("mime-filter"), "形式フィルタは未実装です");
    markUnimplemented(document.getElementById("section-filter"), "見出しフィルタは未実装です");
    const syncAclFromRag = () => {
      const acl = select?.selectedOptions?.[0]?.dataset.acl;
      if (!aclFilter || !acl) return;
      if (![...aclFilter.options].some((option) => option.value === acl)) {
        const option = document.createElement("option");
        option.value = acl;
        option.textContent = acl;
        aclFilter.appendChild(option);
      }
      aclFilter.value = acl;
    };
    syncAclFromRag();
    select?.addEventListener("change", syncAclFromRag);

    const box = document.querySelector(".search-box");
    const input = box?.querySelector("input");
    const button = box?.querySelector("button");
    if (!input || !button) return;
    button.removeAttribute("data-toast");
    button.removeAttribute("data-toast-detail");

    const reset = document.getElementById("search-filter-reset");
    reset?.removeAttribute("data-toast");
    reset?.addEventListener("click", () => {
      if (sourceFilter) sourceFilter.selectedIndex = 0;
      syncAclFromRag();
      if (hybridSwitch) hybridSwitch.classList.add("on");
      if (parentSwitch) parentSwitch.classList.add("on");
      if (dedupSwitch) dedupSwitch.classList.add("on");
      toast("フィルターをリセット", "ソース・ACL・Hybrid・Parent を初期化しました");
    });

    async function runSearch() {
      const ragId = select?.value;
      const query = (input.value || "").trim();
      if (!ragId) {
        toast("検索できません", "先にRAGを作成してください");
        return;
      }
      if (!query) {
        toast("クエリが空です", "検索語を入力してください");
        return;
      }
      if (button.dataset.busy === "1") return;
      button.dataset.busy = "1";
      button.disabled = true;
      const started = performance.now();
      const acl = (aclFilter?.value || "").trim();
      const source = (sourceFilter?.value || "").trim();
      const filters = {};
      if (acl) filters.acl_tags = [acl];
      if (source && source !== "すべて") filters.source_types = [source];
      const payload = {
        rag_id: ragId,
        query,
        top_k: 10,
        score_threshold: 0.0,
        filters,
        expand_parent: switchOn(parentSwitch),
        hybrid: switchOn(hybridSwitch),
        dedup_parent: switchOn(dedupSwitch),
        use_reranker: rerankerSwitch && !rerankerSwitch.classList.contains("is-disabled") ? switchOn(rerankerSwitch) : false,
      };
      try {
        const result = await apiJson("POST", "/api/v1/search", payload);
        renderHits(result, performance.now() - started);
        const chipAcl = document.getElementById("chip-acl");
        const chipSource = document.getElementById("chip-source");
        const chipParent = document.getElementById("chip-parent");
        const chipThresh = document.getElementById("chip-threshold");
        if (chipAcl) chipAcl.textContent = `ACL: ${acl || "—"}`;
        if (chipSource) chipSource.textContent = `source: ${source || "すべて"}`;
        if (chipParent) chipParent.textContent = `親文脈: ${payload.expand_parent ? "ON" : "OFF"}`;
        if (chipThresh) chipThresh.textContent = "threshold: 0.00";
      } catch (error) {
        toast("検索失敗", String(error));
      } finally {
        button.dataset.busy = "";
        button.disabled = false;
      }
    }

    button.addEventListener("click", async (event) => {
      event.preventDefault();
      await runSearch();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        runSearch();
      }
    });
  }

  function renderHits(result, ms) {
    const heading = document.getElementById("search-result-heading") || document.querySelector("main .content strong");
    if (heading) heading.textContent = `検索結果 ${result.hits.length}件`;
    const timing = document.getElementById("search-result-timing");
    if (timing) timing.textContent = `${Math.round(ms)} ms · ${escapeHtml(result.alias || "")}`;
    document.querySelectorAll("article.result-card").forEach((node) => node.remove());
    const empty = document.getElementById("search-empty");
    if (empty) {
      empty.hidden = result.hits.length > 0;
      if (!result.hits.length) {
        empty.textContent = "ヒットはありません。ACL・ソース種別・クエリを確認してください。";
      }
    }
    const host = document.getElementById("search-results") || document.querySelector(".grid.two > section") || document.querySelector("main .content");
    result.hits.forEach((hit) => {
      const article = document.createElement("article");
      article.className = "result-card";
      const parent = hit.parent_text ? `<button class="button small secondary" type="button" data-parent>親文脈</button>` : "";
      article.innerHTML = `
        <div class="result-head">
          <span class="rank">#${hit.rank}</span>
          <span class="result-title">${escapeHtml(hit.title || "")}</span>
          <span class="score-chip">${Number(hit.score || 0).toFixed(2)}</span>
        </div>
        <p>${hit.highlight || escapeHtml(hit.snippet || "")}</p>
        <div class="muted">${escapeHtml(hit.section_path || "")} · ${escapeHtml(hit.source_type || "")} · ${escapeHtml(hit.acl_tag || "")} ${parent}</div>`;
      if (hit.parent_text) {
        article.querySelector("[data-parent]")?.addEventListener("click", () => {
          toast("親文脈", String(hit.parent_text).slice(0, 400));
        });
      }
      host.appendChild(article);
    });
    toast("検索しました", `${result.hits.length}件 · ${Math.round(ms)}ms · ${result.hybrid ? "hybrid RRF" : "dense"}`);
    const explainMs = document.getElementById("search-explain-ms");
    if (explainMs) {
      explainMs.textContent = `${Math.round(ms)} ms`;
      explainMs.className = "status-pill success";
    }
    const explainAcl = document.getElementById("search-explain-acl");
    if (explainAcl) {
      const tags = (result.hits[0] && result.hits[0].acl_tag) || document.getElementById("acl-filter")?.value || "—";
      explainAcl.textContent = `${tags} · Qdrant must filter`;
    }
  }

  function bindWizard() {
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.multiple = true;
    fileInput.id = "file-input";
    fileInput.hidden = true;
    const folderInput = document.createElement("input");
    folderInput.type = "file";
    folderInput.multiple = true;
    folderInput.webkitdirectory = true;
    folderInput.setAttribute("webkitdirectory", "");
    folderInput.hidden = true;
    document.body.appendChild(fileInput);
    document.body.appendChild(folderInput);
    // { path, depth }。depth はフォルダ投入時の階層（直下=1、単体ファイル=0）
    const uploaded = [];
    let mysqlObjects = [];


    function aclTag(value) {
      return String(value || "").split("—")[0].trim();
    }

    const ACL_SELECT_IDS = ["new-rag-acl", "file-acl", "mysql-acl"];

    // 作成先(W1)と取り込み対象(W4)の閲覧範囲を常に同じ値に揃える
    function syncAclSelects(fromId) {
      const from = document.getElementById(fromId);
      if (!from) return;
      const tag = aclTag(from.value);
      ACL_SELECT_IDS.filter((id) => id !== fromId).forEach((id) => {
        const select = document.getElementById(id);
        if (!select) return;
        const match = [...select.options].find((option) => aclTag(option.value) === tag);
        if (match) select.value = match.value;
      });
    }

    ACL_SELECT_IDS.forEach((id) => {
      document.getElementById(id)?.addEventListener("change", () => syncAclSelects(id));
    });
    syncAclSelects("new-rag-acl");

    function folderRecursionEnabled() {
      return !(document.getElementById("folder-depth")?.value || "").startsWith("無効");
    }

    function selectedUploadPaths() {
      const recursive = folderRecursionEnabled();
      return uploaded
        .filter((entry) => recursive || entry.depth <= 1)
        .map((entry) => entry.path);
    }

    function fileKind(name) {
      const ext = (String(name).split(".").pop() || "").toLowerCase();
      if (ext === "pdf") return "PDF";
      if (ext === "docx" || ext === "doc") return "DOC";
      if (ext === "xlsx" || ext === "xls") return "XLS";
      if (ext === "csv") return "CSV";
      if (ext === "md") return "MD";
      if (ext === "txt") return "TXT";
      return (ext.slice(0, 4).toUpperCase() || "FILE");
    }

    function renderUploadedFiles() {
      const host = document.getElementById("file-upload-list");
      const tag = document.getElementById("file-scope-tag");
      if (host) host.dataset.fileCount = String(uploaded.length);
      if (tag) tag.textContent = `${uploaded.length}ファイル`;
      if (!host) return;
      if (!uploaded.length) {
        host.innerHTML = "<div class=\"callout\">まだファイルはありません。選択またはドロップしてください。</div>";
        return;
      }
      host.innerHTML = uploaded.map((entry) => {
        const name = String(entry.path).split(/[\\/]/).pop();
        return `<div class="compact-object">
          <span class="file-icon">${escapeHtml(fileKind(name))}</span>
          <div><strong>${escapeHtml(name)}</strong><span class="mono">${escapeHtml(entry.path)}</span></div>
          <span class="status-pill success">準備完了</span>
        </div>`;
      }).join("");
      window.DB2RAGMock?.syncWizardOverview?.();
    }

    async function filesFromDataTransfer(transfer) {
      const items = [...(transfer?.items || [])];
      const collected = [];
      async function readAll(reader) {
        const batch = [];
        for (;;) {
          const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!entries.length) break;
          batch.push(...entries);
        }
        return batch;
      }
      async function walk(entry, prefix) {
        if (!entry) return;
        if (entry.isFile) {
          const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
          const rel = `${prefix}${file.name}`;
          try {
            Object.defineProperty(file, "webkitRelativePath", { value: rel });
          } catch {
            file.relativePath = rel;
          }
          collected.push(file);
          return;
        }
        if (entry.isDirectory) {
          const children = await readAll(entry.createReader());
          for (const child of children) {
            await walk(child, `${prefix}${entry.name}/`);
          }
        }
      }
      const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
      if (entries.length) {
        for (const entry of entries) await walk(entry, "");
        return collected;
      }
      return [...(transfer?.files || [])];
    }

    async function uploadFileList(fileList) {
      const files = [...(fileList || [])];
      if (!files.length) return;
      const body = new FormData();
      // webkitRelativePath を送り、フォルダ構造を保ったまま保存させる
      files.forEach((file) => body.append("files", file, file.webkitRelativePath || file.name));
      const response = await fetch("/api/v1/files/upload", { method: "POST", body });
      const text = await response.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
      if (!response.ok) {
        toast("アップロード失敗", errorDetail(data, text || String(response.status)));
        return;
      }
      const paths = data.paths || [];
      paths.forEach((path, index) => {
        const rel = files[index]?.webkitRelativePath || "";
        const depth = rel ? Math.max(rel.split("/").length - 1, 1) : 0;
        uploaded.push({ path, depth });
      });
      renderUploadedFiles();
      toast("アップロード", `${files.length} ファイル`);
    }

    document.getElementById("pick-files")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      fileInput.click();
    });
    document.getElementById("pick-folder")?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      folderInput.click();
    });
    fileInput.addEventListener("change", async () => {
      await uploadFileList(fileInput.files);
      fileInput.value = "";
    });
    folderInput.addEventListener("change", async () => {
      await uploadFileList(folderInput.files);
      folderInput.value = "";
    });
    window.DB2RAGLive = {
      onFilesDropped: (files) => { uploadFileList(files).catch((error) => toast("アップロード失敗", String(error))); },
      onDrop: (transfer) => {
        filesFromDataTransfer(transfer).then(uploadFileList).catch((error) => toast("アップロード失敗", String(error)));
      },
      uploadedCount: () => uploaded.length,
    };
    renderUploadedFiles();

    function mysqlConfig() {
      const sslRaw = (document.getElementById("ssl-mode")?.value || "DISABLED").split(" ")[0];
      return {
        host: document.getElementById("host")?.value || "127.0.0.1",
        port: Number(document.getElementById("port")?.value || 3307),
        user: document.getElementById("username")?.value || "db2rag_ro",
        password: document.getElementById("password")?.value || "",
        database: document.getElementById("database")?.value || "hr",
        ssl_mode: sslRaw === "DISABLED" ? "DISABLED" : sslRaw,
      };
    }

    function refreshMysqlCopy() {
      const cfg = mysqlConfig();
      const button = document.getElementById("mysql-copy-uri");
      if (button) button.dataset.copy = `mysql://${cfg.user}:***@${cfg.host}:${cfg.port}/${cfg.database}`;
    }

    ["host", "port", "username", "password", "database"].forEach((id) => {
      document.getElementById(id)?.addEventListener("input", refreshMysqlCopy);
      document.getElementById(id)?.addEventListener("change", refreshMysqlCopy);
    });
    refreshMysqlCopy();

    function setMysqlDiag(ok, detail) {
      const host = document.getElementById("mysql-diag");
      if (!host) return;
      host.className = "diagnostic";
      host.innerHTML = ok
        ? `<span class="diagnostic-icon">✓</span><div><strong>接続診断 PASS</strong><span>${escapeHtml(detail)}</span></div>`
        : `<span class="diagnostic-icon">!</span><div><strong>接続診断 FAIL</strong><span>${escapeHtml(detail)}</span></div>`;
    }

    function renderMysqlObjects(objects) {
      const host = document.getElementById("mysql-object-list");
      const count = document.getElementById("mysql-object-count");
      if (count) count.textContent = `${objects.length}件`;
      if (!host) return;
      if (!objects.length) {
        host.innerHTML = "<div class=\"callout warning\">取り込めるテーブル・ビューがありません。</div>";
        return;
      }
      host.innerHTML = objects.map((item) => `
        <label class="compact-object">
          <input class="checkbox" type="checkbox" ${item.selectable ? "checked" : "disabled"} data-object-key="${escapeHtml(item.object_key)}">
          <div><strong>${escapeHtml(item.object_key)}</strong><span>PK ${escapeHtml((item.pk_columns || []).join(",") || "なし")} · ${Number(item.estimated_rows || 0)} rows</span></div>
          <span class="status-pill ${item.selectable ? "success" : "danger"}">${item.selectable ? "準備完了" : "PKなし"}</span>
        </label>`).join("");
    }

    // W3「入力元を確定」: mockup の同期 completeWizardStep を止め、接続結果を待ってから進める
    const step3Section = document.querySelector("[data-wizard-step='3']");
    const step3Next = step3Section?.querySelector("[data-wizard-next]");
    step3Next?.addEventListener("click", async (event) => {
      const source = document.querySelector('[data-choice-group="source-type"].selected')?.dataset.choiceValue;
      if (source === "file") {
        if (!uploaded.length) {
          event.preventDefault();
          event.stopImmediatePropagation();
          toast("ファイルがありません", "ファイルを選択してから確定してください");
        }
        return;
      }
      if (source !== "mysql") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (step3Next.disabled) return;
      step3Next.disabled = true;
      const host = document.getElementById("mysql-object-list");
      try {
        const cfg = mysqlConfig();
        const listed = await apiJson("POST", "/api/v1/mysql/objects", cfg);
        mysqlObjects = listed.objects || [];
        renderMysqlObjects(mysqlObjects);
        toast("接続成功", `${mysqlObjects.length} オブジェクト`);
        setMysqlDiag(true, `${mysqlObjects.length} オブジェクト · ${cfg.host}:${cfg.port}/${cfg.database}`);
        window.DB2RAGMock?.completeWizardStep?.(step3Section);
      } catch (error) {
        mysqlObjects = [];
        if (host) host.innerHTML = "<div class=\"callout warning\">接続に失敗しました。入力元を確認してください。</div>";
        const count = document.getElementById("mysql-object-count");
        if (count) count.textContent = "接続失敗";
        setMysqlDiag(false, String(error));
        toast("MySQL接続に失敗", String(error));
      } finally {
        step3Next.disabled = false;
      }
    }, true);

    // W5 の見積を実データで更新する
    async function updateW5Metrics() {
      const source = document.querySelector('[data-choice-group="source-type"].selected')?.dataset.choiceValue || "file";
      const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
      const fmtBytes = (bytes) => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
      if (source === "mysql") {
        const checked = [...document.querySelectorAll("#mysql-object-list input[data-object-key]:checked")].map((el) => el.dataset.objectKey);
        const records = mysqlObjects.filter((o) => checked.includes(o.object_key)).reduce((sum, o) => sum + Number(o.estimated_rows || 0), 0);
        set("w5-objects", String(checked.length).padStart(2, "0"));
        set("w5-objects-foot", "MySQL tables / views");
        set("w5-records", records.toLocaleString());
        set("w5-records-foot", "information_schema 推定");
        set("w5-chunks", "—");
        set("w5-chunks-foot", "取り込み後に確定");
        set("w5-storage", "—");
        set("w5-storage-foot", "取り込み後に確定");
        return;
      }
      const paths = selectedUploadPaths();
      set("w5-objects", String(paths.length).padStart(2, "0"));
      set("w5-objects-foot", folderRecursionEnabled() ? "ファイル（サブフォルダ含む）" : "ファイル（直下のみ）");
      set("w5-records", "—");
      set("w5-records-foot", "ファイル投入");
      try {
        const est = await apiJson("POST", "/api/v1/capacity/estimate", { source_type: "file", paths });
        set("w5-chunks", Number(est.est_chunks || 0).toLocaleString());
        set("w5-storage", fmtBytes(Number(est.est_storage_bytes || 0)));
        set("w5-storage-foot", `quick factor ${est.factor ?? "—"}`);
      } catch (error) {
        set("w5-chunks", "—");
        set("w5-chunks-foot", "見積失敗");
      }
    }

    document.querySelector("[data-wizard-step='4'] [data-wizard-next]")?.addEventListener("click", (event) => {
      const source = document.querySelector('[data-choice-group="source-type"].selected')?.dataset.choiceValue || "file";
      if (source === "mysql") {
        const checked = [...document.querySelectorAll("#mysql-object-list input[data-object-key]:checked")];
        if (!checked.length) {
          event.preventDefault();
          event.stopImmediatePropagation();
          toast("テーブルが未選択", "取り込み対象を1つ以上選んでください");
          return;
        }
      } else if (!selectedUploadPaths().length) {
        event.preventDefault();
        event.stopImmediatePropagation();
        toast("ファイルがありません", "ファイルを選択してから確定してください");
        return;
      }
      updateW5Metrics().catch(() => {});
    }, true);

    markUnimplemented(document.getElementById("wizard-draft"), "下書き保存は未実装です");
    const ocrSwitch = document.getElementById("ocr-switch");
    if (ocrSwitch) {
      ocrSwitch.classList.remove("on");
      ocrSwitch.classList.add("is-disabled");
      ocrSwitch.title = "OCR は未実装です。スキャンPDFは取り込めません。";
      ocrSwitch.setAttribute("aria-disabled", "true");
    }

    function mysqlAdvanced(tables) {
      const extra = {};
      const sqlRaw = document.getElementById("mysql-custom-sql")?.value?.trim();
      const templateRaw = document.getElementById("mysql-template-json")?.value?.trim();
      const updatedAt = document.getElementById("mysql-updated-at")?.value?.trim();
      if (sqlRaw) {
        const parsed = JSON.parse(sqlRaw);
        if (!Array.isArray(parsed)) throw new Error("カスタムSQLは配列で書いてください");
        extra.custom_sql = parsed.map((item) => ({
          object_key: item.object_key || item.sql_id,
          sql: item.sql,
          pk_columns: item.pk_columns || [],
        }));
      }
      const templates = templateRaw ? JSON.parse(templateRaw) : {};
      if (templateRaw && (!templates || typeof templates !== "object" || Array.isArray(templates))) {
        throw new Error("テンプレートJSONはオブジェクトで書いてください");
      }
      if (updatedAt) {
        tables.forEach((key) => {
          const current = templates[key] && typeof templates[key] === "object" ? { ...templates[key] } : {};
          if (!current.updated_at_column) current.updated_at_column = updatedAt;
          templates[key] = current;
        });
      }
      if (Object.keys(templates).length) extra.mysql_templates = templates;
      return extra;
    }

    const submit = document.getElementById("wizard-submit");
    if (submit) {
      submit.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (submit.dataset.busy === "1") return;
        const mode = document.querySelector('[data-choice-group="rag-mode"].selected')?.dataset.choiceValue || "new";
        const source = document.querySelector('[data-choice-group="source-type"].selected')?.dataset.choiceValue || "file";
        const aclRaw = source === "file"
          ? document.getElementById("file-acl")?.value
          : document.getElementById("mysql-acl")?.value || document.getElementById("new-rag-acl")?.value;
        const payload = {
          mode: mode === "append" ? "append" : "create",
          display_name: document.getElementById("rag-name")?.value || "新規RAG",
          acl_tag: aclRaw || "internal",
          source_type: source,
        };
        if (mode === "append") {
          payload.rag_id = document.getElementById("existing-rag")?.selectedOptions?.[0]?.dataset.ragId;
          if (!payload.rag_id) {
            toast("追加先が未選択", "既存RAGを選んでください");
            return;
          }
        }
        if (source === "mysql") {
          payload.mysql = mysqlConfig();
          payload.tables = [...document.querySelectorAll("#mysql-object-list input[data-object-key]:checked")]
            .map((el) => el.dataset.objectKey)
            .filter(Boolean);
          if (!payload.tables.length) {
            toast("テーブルが未選択", "取り込み対象を1つ以上選んでください");
            return;
          }
          try {
            Object.assign(payload, mysqlAdvanced(payload.tables));
          } catch (error) {
            toast("詳細設定を読めません", String(error));
            return;
          }
        } else {
          payload.paths = selectedUploadPaths();
          if (!payload.paths.length) {
            toast("ファイルがありません", "ファイルを選択してから変換してください");
            return;
          }
        }
        submit.dataset.busy = "1";
        submit.disabled = true;
        const previous = submit.textContent;
        submit.textContent = "作成中…";
        try {
          const created = await apiJson("POST", "/api/v1/jobs", payload);
          toast("ジョブ作成", created.job_id);
          window.location.href = "/jobs";
        } catch (error) {
          toast("ジョブ作成に失敗", String(error));
          submit.dataset.busy = "";
          submit.disabled = false;
          submit.textContent = previous;
        }
      });
    }

    loadRags().then((rags) => {
      const select = document.getElementById("existing-rag");
      if (!select) return;
      select.innerHTML = rags.map((rag) =>
        `<option value="${escapeHtml(rag.display_name)}" data-rag-id="${rag.rag_id}">${escapeHtml(rag.display_name)}</option>`
      ).join("") || `<option>RAGなし</option>`;
    }).catch(() => {});
  }

  const kind = page();
  if (kind === "list") bindList().catch((error) => toast("一覧取得失敗", String(error)));
  if (kind === "jobs") bindJobs().catch((error) => toast("ジョブ取得失敗", String(error)));
  if (kind === "progress") bindProgress().catch(() => {});
  if (kind === "search") bindSearch().catch((error) => toast("検索画面初期化失敗", String(error)));
  if (kind === "wizard") bindWizard();
})();
