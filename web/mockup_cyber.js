(() => {
  const root = document.documentElement;
  const body = document.body;
  const themeButton = document.getElementById("theme-toggle");
  const fontLabel = document.getElementById("font-size-label");
  const fontSizes = [12, 14, 16, 18];
  let fontIndex = 1;

  function applyTheme(theme) {
    const isLight = theme === "light";
    root.classList.toggle("light", isLight);
    if (themeButton) {
      themeButton.textContent = isLight ? "☀" : "🌙";
      themeButton.setAttribute("aria-label", isLight ? "ダークモードに切り替える" : "ライトモードに切り替える");
    }
  }

  function applyFontSize(index) {
    fontIndex = Math.max(0, Math.min(fontSizes.length - 1, index));
    root.style.fontSize = `${fontSizes[fontIndex]}px`;
    if (fontLabel) {
      fontLabel.textContent = `${fontSizes[fontIndex]}px`;
    }
  }

  document.querySelectorAll("a.nav-link").forEach((link) => {
    const href = link.getAttribute("href") || "";
    const text = link.textContent || "";
    if (href.includes("progress") || /進捗|進歩/.test(text)) {
      link.remove();
    }
  });

  function showToast(title, message) {
    const region = document.getElementById("toast-region");
    if (!region) return;

    const toast = document.createElement("div");
    toast.className = "toast";
    toast.innerHTML = `<strong>${title}</strong><span>${message}</span>`;
    region.appendChild(toast);
    window.setTimeout(() => toast.remove(), 3200);
  }

  const savedTheme = window.localStorage.getItem("db2rag-mock-theme");
  applyTheme(savedTheme === "light" ? "light" : "dark");

  themeButton?.addEventListener("click", () => {
    const nextTheme = root.classList.contains("light") ? "dark" : "light";
    applyTheme(nextTheme);
    window.localStorage.setItem("db2rag-mock-theme", nextTheme);
  });

  document.getElementById("font-plus")?.addEventListener("click", () => applyFontSize(fontIndex + 1));
  document.getElementById("font-minus")?.addEventListener("click", () => applyFontSize(fontIndex - 1));
  applyFontSize(fontIndex);

  document.getElementById("collapse-sidebar")?.addEventListener("click", () => {
    body.classList.add("sidebar-collapsed");
  });

  document.getElementById("sidebar-balloon")?.addEventListener("click", () => {
    body.classList.remove("sidebar-collapsed");
  });

  const sidebar = document.getElementById("sidebar");
  const resizer = document.getElementById("sidebar-resizer");
  if (sidebar && resizer) {
    let dragging = false;

    resizer.addEventListener("pointerdown", (event) => {
      if (event.target.closest("button")) return;
      dragging = true;
      resizer.classList.add("dragging");
      resizer.setPointerCapture(event.pointerId);
    });

    resizer.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const width = Math.max(184, Math.min(380, event.clientX));
      root.style.setProperty("--sidebar-width", `${width}px`);
    });

    const stopDragging = () => {
      dragging = false;
      resizer.classList.remove("dragging");
    };
    resizer.addEventListener("pointerup", stopDragging);
    resizer.addEventListener("pointercancel", stopDragging);
  }

  document.querySelectorAll("[data-toast]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (element.tagName === "A" && element.getAttribute("href")?.endsWith(".html")) return;
      event.preventDefault();
      showToast(element.dataset.toast || "操作を受け付けました", element.dataset.toastDetail || "これは確認用の静的モックです。");
    });
  });

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(value);
        showToast("コピーしました", value);
      } catch {
        showToast("コピー内容", value);
      }
    });
  });

  const wizardSections = [...document.querySelectorAll("[data-wizard-step]")]
    .sort((left, right) => Number(left.dataset.wizardStep) - Number(right.dataset.wizardStep));

  function selectedChoiceValue(group) {
    return document.querySelector(`[data-choice-group="${group}"].selected`)?.dataset.choiceValue || "";
  }

  function uploadedFileCount() {
    const liveCount = window.DB2RAGLive?.uploadedCount?.();
    if (Number.isFinite(liveCount)) return liveCount;
    return Number(document.getElementById("file-upload-list")?.dataset.fileCount || 0);
  }

  function updateChoiceBranches(group, value) {
    document.querySelectorAll(`[data-branch-group="${group}"]`).forEach((panel) => {
      panel.hidden = panel.dataset.branchValue !== value;
    });
  }

  function setWizardSectionOpen(section) {
    if (!section || section.classList.contains("is-locked")) return;
    wizardSections.forEach((candidate) => {
      const isTarget = candidate === section;
      candidate.classList.toggle("is-open", isTarget);
      candidate.querySelector("[data-wizard-toggle]")?.setAttribute("aria-expanded", String(isTarget));
    });
    window.setTimeout(() => section.scrollIntoView({ behavior: "smooth", block: "nearest" }), 100);
  }

  function invalidateLaterWizardSteps(section) {
    const currentStep = Number(section.dataset.wizardStep);
    wizardSections.forEach((candidate) => {
      if (Number(candidate.dataset.wizardStep) <= currentStep) return;
      candidate.classList.remove("is-complete", "is-open");
      candidate.classList.add("is-locked");
      candidate.querySelector("[data-wizard-toggle]")?.setAttribute("aria-expanded", "false");
      const summary = candidate.querySelector("[data-wizard-summary]");
      if (summary) summary.textContent = "前の項目を確定してください";
    });
  }

  function buildWizardSummary(section) {
    switch (section.dataset.wizardStep) {
      case "1":
        return selectedChoiceValue("rag-mode") === "append"
          ? `既存へ追加: ${document.getElementById("existing-rag")?.value || "未選択"}`
          : `新規作成: ${document.getElementById("rag-name")?.value || "名称未入力"}`;
      case "2":
        return selectedChoiceValue("source-type") === "file"
          ? "ファイル / フォルダ"
          : "MySQL 業務データベース";
      case "3":
        return selectedChoiceValue("source-type") === "file"
          ? (uploadedFileCount() ? `${uploadedFileCount()}ファイル` : "ファイル未選択")
          : "接続確認へ進む";
      case "4":
        return selectedChoiceValue("source-type") === "file"
          ? `${uploadedFileCount() || 0}ファイル · ACL private`
          : "オブジェクト選択へ進む";
      case "5":
        return "容量確認済み · 変換開始可能";
      default:
        return "完了";
    }
  }

  function syncWizardOverview() {
    const mode = selectedChoiceValue("rag-mode");
    const source = selectedChoiceValue("source-type");
    const target = mode === "append"
      ? document.getElementById("existing-rag")?.value
      : document.getElementById("rag-name")?.value;
    const values = {
      "overview-mode": mode === "append" ? "既存RAGへ追加" : "新規RAGを作成",
      "overview-target": target || "未確定",
      "overview-source": source === "file" ? "ファイル / フォルダ" : "MySQL",
      "overview-scope": source === "file" ? `${uploadedFileCount()}ファイル` : "オブジェクト選択",
      "overview-size": source === "file" ? "ファイル確定後に算出" : "接続後に算出",
    };
    Object.entries(values).forEach(([id, value]) => {
      const element = document.getElementById(id);
      if (element) element.textContent = value;
    });
  }

  function completeWizardStep(section) {
    if (!section) return;
    section.classList.add("is-complete");
    const summary = section.querySelector("[data-wizard-summary]");
    if (summary) summary.textContent = buildWizardSummary(section);
    const currentIndex = wizardSections.indexOf(section);
    const nextSection = wizardSections[currentIndex + 1];
    if (nextSection) {
      nextSection.classList.remove("is-locked");
      setWizardSectionOpen(nextSection);
    } else {
      section.classList.remove("is-open");
      section.querySelector("[data-wizard-toggle]")?.setAttribute("aria-expanded", "false");
    }
    syncWizardOverview();
  }

  document.querySelectorAll(".choice-card").forEach((card) => {
    card.addEventListener("click", () => {
      const group = card.dataset.choiceGroup;
      if (!group) return;
      document.querySelectorAll(`[data-choice-group="${group}"]`).forEach((peer) => {
        peer.classList.toggle("selected", peer === card);
      });
      updateChoiceBranches(group, card.dataset.choiceValue || "");
      const section = card.closest("[data-wizard-step]");
      if (section?.classList.contains("is-complete")) {
        section.classList.remove("is-complete");
        invalidateLaterWizardSteps(section);
      }
      syncWizardOverview();
      if (card.dataset.autoNext === "true") {
        window.setTimeout(() => completeWizardStep(section), 180);
      }
    });
  });

  wizardSections.forEach((section) => {
    section.querySelector("[data-wizard-toggle]")?.addEventListener("click", () => {
      if (section.classList.contains("is-locked")) return;
      if (section.classList.contains("is-open")) {
        section.classList.remove("is-open");
        section.querySelector("[data-wizard-toggle]")?.setAttribute("aria-expanded", "false");
      } else {
        setWizardSectionOpen(section);
      }
    });

    section.querySelectorAll("[data-wizard-next]").forEach((button) => {
      button.addEventListener("click", () => completeWizardStep(section));
    });

    section.querySelectorAll("[data-wizard-back]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = wizardSections.find(
          (candidate) => candidate.dataset.wizardStep === button.dataset.wizardBack,
        );
        setWizardSectionOpen(target);
      });
    });

    section.querySelectorAll("input, select, textarea").forEach((control) => {
      control.addEventListener("change", () => {
        if (section.classList.contains("is-complete")) {
          section.classList.remove("is-complete");
          invalidateLaterWizardSteps(section);
        }
        syncWizardOverview();
      });
    });
  });

  document.querySelectorAll("[data-choice-group]").forEach((card) => {
    if (card.classList.contains("selected")) {
      updateChoiceBranches(card.dataset.choiceGroup, card.dataset.choiceValue || "");
    }
  });
  syncWizardOverview();

  document.querySelectorAll("[data-tab-target]").forEach((tab) => {
    tab.addEventListener("click", () => {
      const container = tab.closest("[data-tabs]");
      if (!container) return;
      container.querySelectorAll("[data-tab-target]").forEach((peer) => peer.classList.remove("active"));
      container.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
      tab.classList.add("active");
      container.querySelector(`#${tab.dataset.tabTarget}`)?.classList.add("active");
    });
  });

  const dropzone = document.querySelector(".dropzone");
  if (dropzone) {
    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("drag-over");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("drag-over");
        if (eventName === "drop") {
          if (window.DB2RAGLive?.onDrop) {
            window.DB2RAGLive.onDrop(event.dataTransfer);
          } else {
            window.DB2RAGLive?.onFilesDropped?.(event.dataTransfer?.files);
          }
        }
      });
    });
  }

  const jobHover = document.getElementById("job-detail-overlay");
  let jobHoverPinned = false;
  let jobHoverAnchor = null;

  function closeJobHover() {
    if (!jobHover) return;
    jobHoverPinned = false;
    jobHover.classList.remove("is-open", "is-pinned");
    jobHover.setAttribute("hidden", "");
  }

  function placeJobHover(anchor) {
    if (!jobHover) return;
    if (anchor) jobHoverAnchor = anchor;
    jobHover.removeAttribute("hidden");
    jobHover.classList.add("is-open", "is-pinned");
    jobHoverPinned = true;
    const target = jobHoverAnchor;
    if (!target) return;

    const place = () => {
      const view = window.visualViewport;
      const viewTop = view ? view.offsetTop : 0;
      const viewLeft = view ? view.offsetLeft : 0;
      const viewWidth = view ? view.width : window.innerWidth;
      const viewHeight = view ? view.height : window.innerHeight;
      const margin = 12;
      const topbar = document.querySelector(".topbar");
      const topMin = Math.max(
        viewTop + margin,
        Math.ceil((topbar?.getBoundingClientRect().bottom || 0) + 8),
      );
      const bottomMax = viewTop + viewHeight - margin;
      const maxHeight = Math.max(160, bottomMax - topMin);
      jobHover.style.maxHeight = `${maxHeight}px`;
      const maxWidth = Math.min(520, viewWidth - margin * 2);
      jobHover.style.width = `${maxWidth}px`;
      void jobHover.offsetHeight;

      const rect = target.getBoundingClientRect();
      const width = Math.min(jobHover.offsetWidth || maxWidth, maxWidth);
      const height = Math.min(jobHover.offsetHeight || 280, maxHeight);

      let left = rect.right - width;
      left = Math.min(Math.max(viewLeft + margin, left), viewLeft + viewWidth - width - margin);

      let top = rect.bottom + 8;
      if (top + height > bottomMax) {
        top = rect.top - height - 8;
      }
      top = Math.min(Math.max(top, topMin), Math.max(topMin, bottomMax - height));

      jobHover.style.left = `${left}px`;
      jobHover.style.top = `${top}px`;
    };
    window.requestAnimationFrame(() => window.requestAnimationFrame(place));
  }

  function pinJobHover(anchor) {
    if (jobHoverPinned && jobHoverAnchor === anchor) {
      closeJobHover();
      return;
    }
    placeJobHover(anchor);
  }

  document.getElementById("job-hover-close")?.addEventListener("click", (event) => {
    event.stopPropagation();
    closeJobHover();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeJobHover();
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-job-detail]");
    if (button && button.dataset.jobLive !== "true") {
      event.preventDefault();
      event.stopPropagation();
      const idLabel = document.getElementById("job-hover-id");
      if (idLabel && button.dataset.jobId) idLabel.textContent = button.dataset.jobId;
      pinJobHover(button);
      return;
    }
    if (jobHoverPinned && jobHover && !jobHover.contains(event.target) && !event.target.closest("[data-job-detail]")) {
      closeJobHover();
    }
  });

  window.DB2RAGMock = {
    showToast,
    closeJobHover,
    placeJobHover,
    pinJobHover,
    syncWizardOverview,
    completeWizardStep,
  };
})();
