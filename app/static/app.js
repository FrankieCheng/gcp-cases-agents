// Copyright 2026 Google LLC
// Pure Safe DOM Manipulation Frontend for Google Cloud Support Case Agent

(function () {
  "use strict";

  const appState = {
    csrfToken: "",
    mode: "MOCK",
    forceTestCase: true,
    allowedProjects: [],
    activeProject: "gcp-prod-core-01",
    stateFilter: "ALL",
    classifications: [],
    cases: [],
    selectedCase: null,
    pendingDrafts: [],
  };

  function maskEmailAddress(raw) {
    const cleaned = String(raw || "").trim();
    const atIndex = cleaned.indexOf("@");
    if (atIndex <= 0) {
      return cleaned || "***";
    }
    const localPart = cleaned.slice(0, atIndex);
    const domainPart = cleaned.slice(atIndex + 1);
    return localPart.charAt(0) + "***@" + domainPart;
  }

  function showToast(message, isError) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const item = document.createElement("div");
    item.className = isError ? "toast-item error" : "toast-item";
    item.textContent = String(message || "");
    container.appendChild(item);
    window.setTimeout(function () {
      if (item.parentNode) {
        item.parentNode.removeChild(item);
      }
    }, 4000);
  }

  async function apiRequest(path, options) {
    const opts = options || {};
    const method = (opts.method || "GET").toUpperCase();
    const headers = Object.assign({}, opts.headers || {});

    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = appState.csrfToken;
    }
    if (opts.jsonBody !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    const fetchOptions = {
      method: method,
      headers: headers,
      credentials: "same-origin",
    };
    if (opts.jsonBody !== undefined) {
      fetchOptions.body = JSON.stringify(opts.jsonBody);
    } else if (opts.formData !== undefined) {
      fetchOptions.body = opts.formData;
    }

    const resp = await window.fetch(path, fetchOptions);
    let payload = {};
    try {
      payload = await resp.json();
    } catch (_err) {
      payload = {};
    }
    if (!resp.ok) {
      const errMsg = payload.detail || payload.error || "请求执行失败 (" + resp.status + ")";
      throw new Error(errMsg);
    }
    return payload;
  }

  function renderProjectSelectors() {
    const mainSelect = document.getElementById("project-select");
    const draftSelect = document.getElementById("draft-project-input");
    const pill = document.getElementById("active-project-pill");

    [mainSelect, draftSelect].forEach(function (sel) {
      if (!sel) return;
      sel.replaceChildren();
      appState.allowedProjects.forEach(function (proj) {
        const opt = document.createElement("option");
        opt.value = proj;
        opt.textContent = proj;
        if (proj === appState.activeProject) {
          opt.selected = true;
        }
        sel.appendChild(opt);
      });
    });

    if (pill) {
      pill.textContent = "Project: " + appState.activeProject;
    }
  }

  function renderClassificationOptions() {
    const clsSelect = document.getElementById("draft-classification-input");
    if (!clsSelect) return;
    clsSelect.replaceChildren();
    appState.classifications.forEach(function (cls) {
      const opt = document.createElement("option");
      opt.value = cls.id;
      opt.textContent = cls.display_name;
      opt.setAttribute("data-display-name", cls.display_name);
      clsSelect.appendChild(opt);
    });
  }

  function renderModeButtons() {
    const mockBtn = document.getElementById("btn-mode-mock");
    const liveBtn = document.getElementById("btn-mode-live");
    if (!mockBtn || !liveBtn) return;
    if (appState.mode === "LIVE") {
      liveBtn.classList.add("active");
      mockBtn.classList.remove("active");
    } else {
      mockBtn.classList.add("active");
      liveBtn.classList.remove("active");
    }
  }

  function renderCaseList() {
    const container = document.getElementById("case-list-container");
    if (!container) return;
    container.replaceChildren();

    if (!appState.cases || appState.cases.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "当前筛选条件下暂无工单记录";
      container.appendChild(empty);
      return;
    }

    appState.cases.forEach(function (item) {
      const card = document.createElement("div");
      const isSelected = appState.selectedCase && appState.selectedCase.name === item.name;
      card.className = isSelected ? "case-card selected" : "case-card";

      const topRow = document.createElement("div");
      topRow.className = "case-card-top";

      const badgeGroup = document.createElement("div");
      badgeGroup.className = "detail-badges";

      const sevBadge = document.createElement("span");
      sevBadge.className = "sev-badge sev-" + item.severity;
      sevBadge.textContent = item.severity;
      badgeGroup.appendChild(sevBadge);

      const stateBadge = document.createElement("span");
      stateBadge.className = item.state === "CLOSED" ? "state-badge state-CLOSED" : "state-badge";
      stateBadge.textContent = item.state;
      badgeGroup.appendChild(stateBadge);

      if (item.escalated) {
        const escBadge = document.createElement("span");
        escBadge.className = "esc-badge";
        escBadge.textContent = "ESCALATED";
        badgeGroup.appendChild(escBadge);
      }

      const idSpan = document.createElement("span");
      idSpan.className = "case-card-meta";
      idSpan.textContent = "#" + item.case_id;

      topRow.appendChild(badgeGroup);
      topRow.appendChild(idSpan);

      const titleDiv = document.createElement("div");
      titleDiv.className = "case-card-title";
      titleDiv.textContent = item.display_name;

      const metaDiv = document.createElement("div");
      metaDiv.className = "case-card-meta";
      metaDiv.textContent = item.classification ? item.classification.display_name : "";

      card.appendChild(topRow);
      card.appendChild(titleDiv);
      card.appendChild(metaDiv);

      card.addEventListener("click", function () {
        loadCaseDetails(item.project_id, item.case_id);
      });

      container.appendChild(card);
    });
  }

  function renderSelectedCaseDetail() {
    const emptyBox = document.getElementById("case-detail-empty");
    const contentBox = document.getElementById("case-detail-content");
    if (!emptyBox || !contentBox) return;

    const c = appState.selectedCase;
    if (!c) {
      emptyBox.classList.remove("hidden");
      contentBox.classList.add("hidden");
      return;
    }

    emptyBox.classList.add("hidden");
    contentBox.classList.remove("hidden");

    const sevBadge = document.getElementById("detail-severity-badge");
    if (sevBadge) {
      sevBadge.className = "sev-badge sev-" + c.severity;
      sevBadge.textContent = c.severity + " (" + c.priority + ")";
    }

    const stateBadge = document.getElementById("detail-state-badge");
    if (stateBadge) {
      stateBadge.className = c.state === "CLOSED" ? "state-badge state-CLOSED" : "state-badge";
      stateBadge.textContent = c.state;
    }

    const escBadge = document.getElementById("detail-escalated-badge");
    if (escBadge) {
      if (c.escalated) {
        escBadge.classList.remove("hidden");
        escBadge.textContent = "ESCALATED (" + (c.escalation_reason || "URGENT") + ")";
      } else {
        escBadge.classList.add("hidden");
      }
    }

    const testBadge = document.getElementById("detail-testcase-badge");
    if (testBadge) {
      testBadge.textContent = c.test_case ? "testCase=True" : "testCase=False";
    }

    const titleEl = document.getElementById("detail-title");
    if (titleEl) titleEl.textContent = c.display_name;

    const nameEl = document.getElementById("detail-case-name");
    if (nameEl) nameEl.textContent = c.name;

    const clsEl = document.getElementById("detail-classification");
    if (clsEl) {
      clsEl.textContent = c.classification ? c.classification.display_name : "";
    }

    const subEl = document.getElementById("detail-subscribers");
    if (subEl) {
      const maskedList = (c.subscriber_emails || []).map(maskEmailAddress);
      subEl.textContent = maskedList.length > 0 ? maskedList.join(", ") : "未配置订阅人";
    }

    const updatedEl = document.getElementById("detail-updated");
    if (updatedEl) {
      updatedEl.textContent = (c.time_zone || "UTC") + " · " + (c.update_time || "");
    }

    const descEl = document.getElementById("detail-description");
    if (descEl) {
      descEl.textContent = c.description;
    }

    // Attachments list
    const attContainer = document.getElementById("detail-attachments-list");
    if (attContainer) {
      attContainer.replaceChildren();
      const attachments = c.attachments || [];
      if (attachments.length === 0) {
        const noAtt = document.createElement("div");
        noAtt.className = "case-card-meta";
        noAtt.textContent = "暂无已上传的诊断附件";
        attContainer.appendChild(noAtt);
      } else {
        attachments.forEach(function (att) {
          const row = document.createElement("div");
          row.className = "attachment-item";

          const info = document.createElement("span");
          info.textContent = att.filename + " (" + att.size_bytes + " B)";

          const link = document.createElement("a");
          link.className = "attachment-link";
          link.setAttribute("href", att.download_url);
          link.setAttribute("download", att.filename);
          link.textContent = "安全下载";

          row.appendChild(info);
          row.appendChild(link);
          attContainer.appendChild(row);
        });
      }
    }

    // Comments timeline
    const cmtContainer = document.getElementById("detail-comments-list");
    if (cmtContainer) {
      cmtContainer.replaceChildren();
      const comments = c.comments || [];
      comments.forEach(function (cmt) {
        const card = document.createElement("div");
        card.className = cmt.author_type === "GOOGLE_SUPPORT" ? "comment-card support" : "comment-card";

        const meta = document.createElement("div");
        meta.className = "comment-meta";

        const authorSpan = document.createElement("span");
        authorSpan.textContent = maskEmailAddress(cmt.author);

        const timeSpan = document.createElement("span");
        timeSpan.textContent = cmt.create_time;

        meta.appendChild(authorSpan);
        meta.appendChild(timeSpan);

        const bodyDiv = document.createElement("div");
        bodyDiv.textContent = cmt.body;

        card.appendChild(meta);
        card.appendChild(bodyDiv);
        cmtContainer.appendChild(card);
      });
    }
  }

  function appendChatBubble(role, text, toolCalls) {
    const stream = document.getElementById("chat-stream");
    if (!stream) return;

    const bubble = document.createElement("div");
    bubble.className = role === "user" ? "chat-bubble user" : "chat-bubble assistant";

    const textNode = document.createElement("div");
    textNode.textContent = String(text || "");
    bubble.appendChild(textNode);

    if (Array.isArray(toolCalls) && toolCalls.length > 0) {
      const traceList = document.createElement("div");
      traceList.className = "tool-trace-list";
      toolCalls.forEach(function (tc) {
        const pill = document.createElement("span");
        pill.className = "tool-pill";
        pill.textContent = "Tool: " + tc.tool;
        traceList.appendChild(pill);
      });
      bubble.appendChild(traceList);
    }

    stream.appendChild(bubble);
    stream.scrollTop = stream.scrollHeight;
  }

  function renderPendingDraftCards() {
    const dock = document.getElementById("hitl-cards-dock");
    if (!dock) return;
    dock.replaceChildren();

    appState.pendingDrafts.forEach(function (draft) {
      const card = document.createElement("div");
      card.className = "hitl-card";

      const header = document.createElement("div");
      header.className = "hitl-card-header";

      const titleSpan = document.createElement("span");
      if (draft.draft_type === "CREATE_CASE") {
        titleSpan.textContent = "🛡️ 待确认新建工单草稿 (" + draft.draft_id + ")";
      } else if (draft.draft_type === "CLOSE_CASE") {
        titleSpan.textContent = "🛡️ 待确认关闭工单草稿 (" + draft.draft_id + ")";
      } else {
        titleSpan.textContent = "🛡️ 待确认工单升级草稿 (" + draft.draft_id + ")";
      }

      const modeTag = document.createElement("span");
      modeTag.textContent = "HITL 护栏拦截中";
      header.appendChild(titleSpan);
      header.appendChild(modeTag);
      card.appendChild(header);

      const payload = draft.payload || {};

      if (draft.draft_type === "CREATE_CASE") {
        const grid = document.createElement("div");
        grid.className = "hitl-grid";

        const titleInput = document.createElement("input");
        titleInput.type = "text";
        titleInput.className = "text-input";
        titleInput.value = payload.display_name || "";

        const sevSelect = document.createElement("select");
        sevSelect.className = "select-input";
        ["S1", "S2", "S3", "S4"].forEach(function (s) {
          const opt = document.createElement("option");
          opt.value = s;
          opt.textContent = "严重级别: " + s;
          if (payload.severity === s) opt.selected = true;
          sevSelect.appendChild(opt);
        });

        grid.appendChild(titleInput);
        grid.appendChild(sevSelect);
        card.appendChild(grid);

        const descInput = document.createElement("textarea");
        descInput.className = "chat-textarea";
        descInput.rows = 2;
        descInput.value = payload.description || "";
        card.appendChild(descInput);

        const clsMeta = document.createElement("div");
        clsMeta.className = "case-card-meta";
        clsMeta.textContent =
          "Project: " +
          draft.project_id +
          " | 分类: " +
          (payload.classification_display_name || "") +
          " | testCase=" +
          Boolean(payload.test_case);
        card.appendChild(clsMeta);

        const actions = document.createElement("div");
        actions.className = "hitl-actions";

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-outline-sm";
        cancelBtn.textContent = "取消草稿";
        cancelBtn.addEventListener("click", function () {
          handleCancelDraft(draft.draft_id);
        });

        const confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "btn btn-primary";
        confirmBtn.textContent = "✅ 确认提交工单 (cases.create)";
        confirmBtn.addEventListener("click", function () {
          handleConfirmDraft(draft.draft_id, {
            confirmed: true,
            display_name: titleInput.value,
            description: descInput.value,
            severity: sevSelect.value,
          });
        });

        actions.appendChild(cancelBtn);
        actions.appendChild(confirmBtn);
        card.appendChild(actions);
      } else {
        const isClose = draft.draft_type === "CLOSE_CASE";
        const info = document.createElement("div");
        info.className = "case-card-meta";
        info.textContent = isClose
          ? "目标工单: " + draft.case_name + " | 关闭原因: " + (payload.reason || "")
          : "目标工单: " +
            draft.case_name +
            " | 原因: " +
            (payload.reason || "") +
            " | 说明: " +
            (payload.justification || "");
        card.appendChild(info);

        const actions = document.createElement("div");
        actions.className = "hitl-actions";

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-outline-sm";
        cancelBtn.textContent = isClose ? "取消关闭" : "取消升级";
        cancelBtn.addEventListener("click", function () {
          handleCancelDraft(draft.draft_id);
        });

        const confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "btn btn-primary";
        confirmBtn.textContent = isClose
          ? "✅ 确认关闭工单 (cases.close)"
          : "✅ 确认升级工单 (cases.escalate)";
        confirmBtn.addEventListener("click", function () {
          handleConfirmDraft(draft.draft_id, { confirmed: true });
        });

        actions.appendChild(cancelBtn);
        actions.appendChild(confirmBtn);
        card.appendChild(actions);
      }

      dock.appendChild(card);
    });
  }

  async function loadCasesForActiveProject() {
    try {
      const data = await apiRequest(
        "/api/cases?project_id=" +
          encodeURIComponent(appState.activeProject) +
          "&state_filter=" +
          encodeURIComponent(appState.stateFilter)
      );
      appState.cases = data.cases || [];
      renderCaseList();
      if (!appState.selectedCase && appState.cases.length > 0) {
        await loadCaseDetails(appState.cases[0].project_id, appState.cases[0].case_id);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function loadCaseDetails(projectId, caseId) {
    try {
      const data = await apiRequest(
        "/api/cases/" + encodeURIComponent(projectId) + "/" + encodeURIComponent(caseId)
      );
      appState.selectedCase = data.case;
      renderCaseList();
      renderSelectedCaseDetail();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function handleConfirmDraft(draftId, confirmBody) {
    try {
      const res = await apiRequest(
        "/api/drafts/" + encodeURIComponent(draftId) + "/confirm",
        {
          method: "POST",
          jsonBody: confirmBody,
        }
      );
      appState.pendingDrafts = appState.pendingDrafts.filter(function (d) {
        return d.draft_id !== draftId;
      });
      renderPendingDraftCards();

      if (res.case) {
        appState.selectedCase = res.case;
        renderSelectedCaseDetail();
        appendChatBubble(
          "assistant",
          "✅ 已通过 HITL 确认并成功执行：" + res.case.name + " (" + res.case.display_name + ")",
          [{ tool: "user_confirmed:" + res.draft_type }]
        );
      }
      await loadCasesForActiveProject();
      showToast("草稿已确认并执行成功", false);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function handleCancelDraft(draftId) {
    try {
      await apiRequest("/api/drafts/" + encodeURIComponent(draftId) + "/cancel", {
        method: "POST",
      });
      appState.pendingDrafts = appState.pendingDrafts.filter(function (d) {
        return d.draft_id !== draftId;
      });
      renderPendingDraftCards();
      showToast("已取消待确认草稿 " + draftId, false);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function sendChatMessage(messageText) {
    const trimmed = String(messageText || "").trim();
    if (!trimmed) return;

    appendChatBubble("user", trimmed, []);
    try {
      const res = await apiRequest("/api/chat", {
        method: "POST",
        jsonBody: {
          message: trimmed,
          project_id: appState.activeProject,
          selected_case_name: appState.selectedCase ? appState.selectedCase.name : null,
        },
      });
      appendChatBubble("assistant", res.reply, res.tool_calls || []);

      if (Array.isArray(res.pending_drafts) && res.pending_drafts.length > 0) {
        res.pending_drafts.forEach(function (d) {
          const exists = appState.pendingDrafts.some(function (existing) {
            return existing.draft_id === d.draft_id;
          });
          if (!exists) {
            appState.pendingDrafts.unshift(d);
          }
        });
        renderPendingDraftCards();
      }
      if (res.updated_case) {
        appState.selectedCase = res.updated_case;
        renderSelectedCaseDetail();
      }
      await loadCasesForActiveProject();
    } catch (err) {
      appendChatBubble("assistant", "⚠️ 请求未通过校验: " + err.message, []);
      showToast(err.message, true);
    }
  }

  async function initializePortal() {
    try {
      const sessionData = await apiRequest("/api/session");
      appState.csrfToken = sessionData.csrf_token;
      appState.mode = sessionData.mode || "MOCK";
      appState.forceTestCase = Boolean(sessionData.force_test_case);
      appState.allowedProjects = sessionData.allowed_projects || [];
      appState.activeProject = sessionData.default_project || "gcp-prod-core-01";
      appState.classifications = sessionData.classifications || [];
      appState.pendingDrafts = sessionData.pending_drafts || [];

      renderProjectSelectors();
      renderClassificationOptions();
      renderModeButtons();
      renderPendingDraftCards();

      appendChatBubble(
        "assistant",
        "👋 欢迎使用 Google Cloud Support Case 智能管理门户！\n" +
          "当前运行于 " +
          appState.mode +
          " 模式，已启用 Project RBAC 白名单与创建/升级 HITL 结构化确认卡片护栏。\n" +
          "您可以点击上方快捷指令、在左侧切换项目查看工单，或直接告诉我您需要提报的故障问题。",
        []
      );

      await loadCasesForActiveProject();
    } catch (err) {
      showToast("初始化会话失败: " + err.message, true);
    }
  }

  function bindEvents() {
    const projectSelect = document.getElementById("project-select");
    if (projectSelect) {
      projectSelect.addEventListener("change", function () {
        appState.activeProject = projectSelect.value;
        appState.selectedCase = null;
        renderProjectSelectors();
        renderSelectedCaseDetail();
        loadCasesForActiveProject();
      });
    }

    const filterButtons = document.querySelectorAll(".filter-tab");
    filterButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        filterButtons.forEach(function (b) {
          b.classList.remove("active");
        });
        btn.classList.add("active");
        appState.stateFilter = btn.getAttribute("data-filter") || "ALL";
        loadCasesForActiveProject();
      });
    });

    const mockBtn = document.getElementById("btn-mode-mock");
    const liveBtn = document.getElementById("btn-mode-live");
    [
      { btn: mockBtn, mode: "MOCK" },
      { btn: liveBtn, mode: "LIVE" },
    ].forEach(function (entry) {
      if (!entry.btn) return;
      entry.btn.addEventListener("click", async function () {
        try {
          const res = await apiRequest("/api/mode", {
            method: "POST",
            jsonBody: { mode: entry.mode },
          });
          appState.mode = res.mode;
          renderModeButtons();
          showToast("已切换至 " + res.mode + " 模式", false);
        } catch (err) {
          showToast(err.message, true);
        }
      });
    });

    const logoutBtn = document.getElementById("btn-logout");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", async function () {
        try {
          await apiRequest("/api/logout", { method: "POST" });
        } catch (_e) {
          // Ignore error during logout cleanup
        }
        appState.csrfToken = "";
        appState.cases = [];
        appState.selectedCase = null;
        appState.pendingDrafts = [];
        window.location.reload();
      });
    }

    const chatForm = document.getElementById("chat-form");
    const chatInput = document.getElementById("chat-input");
    if (chatForm && chatInput) {
      chatForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        const text = chatInput.value;
        chatInput.value = "";
        sendChatMessage(text);
      });
      chatInput.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter") return;
        // Ignore Enter while an IME (e.g. Chinese pinyin) is composing;
        // that Enter only confirms the candidate text.
        if (ev.isComposing || ev.keyCode === 229) return;
        // Plain Enter / Shift+Enter insert a newline (default textarea behavior).
        // Ctrl+Enter (Cmd+Enter on macOS) sends the message.
        if (ev.ctrlKey || ev.metaKey) {
          ev.preventDefault();
          const text = chatInput.value;
          if (!text.trim()) return;
          chatInput.value = "";
          sendChatMessage(text);
        }
      });
    }

    const chips = document.querySelectorAll(".chip-btn");
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        const promptText = chip.getAttribute("data-prompt");
        if (promptText) {
          sendChatMessage(promptText);
        }
      });
    });

    // Create Draft Modal
    const createModal = document.getElementById("modal-create-draft");
    const openCreateBtn = document.getElementById("btn-new-case-modal");
    const closeCreateBtn = document.getElementById("btn-close-create-modal");
    const cancelCreateBtn = document.getElementById("btn-cancel-create-modal");
    const createForm = document.getElementById("form-create-draft");

    if (openCreateBtn && createModal) {
      openCreateBtn.addEventListener("click", function () {
        createModal.classList.remove("hidden");
      });
    }
    [closeCreateBtn, cancelCreateBtn].forEach(function (b) {
      if (b && createModal) {
        b.addEventListener("click", function () {
          createModal.classList.add("hidden");
        });
      }
    });
    if (createForm && createModal) {
      createForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        const clsSelect = document.getElementById("draft-classification-input");
        const selectedOpt = clsSelect.options[clsSelect.selectedIndex];
        try {
          const res = await apiRequest("/api/drafts/case", {
            method: "POST",
            jsonBody: {
              project_id: document.getElementById("draft-project-input").value,
              classification_id: clsSelect.value,
              classification_display_name: selectedOpt ? selectedOpt.textContent : "",
              severity: document.getElementById("draft-severity-input").value,
              display_name: document.getElementById("draft-title-input").value,
              description: document.getElementById("draft-desc-input").value,
              subscriber_emails: document.getElementById("draft-emails-input").value,
              time_zone: "Asia/Shanghai",
            },
          });
          createModal.classList.add("hidden");
          createForm.reset();
          if (res.draft) {
            appState.pendingDrafts.unshift(res.draft);
            renderPendingDraftCards();
            appendChatBubble(
              "assistant",
              "🛡️ 已创建待确认工单草稿 " + res.draft.draft_id + "，请在下方确认卡片核对并点击「确认提交工单」。",
              [{ tool: "prepare_case_draft" }]
            );
          }
        } catch (err) {
          showToast(err.message, true);
        }
      });
    }

    // Escalate Modal
    const escModal = document.getElementById("modal-escalate-draft");
    const openEscBtn = document.getElementById("btn-open-escalate");
    const closeEscBtn = document.getElementById("btn-close-escalate-modal");
    const cancelEscBtn = document.getElementById("btn-cancel-escalate-modal");
    const escForm = document.getElementById("form-escalate-draft");

    if (openEscBtn && escModal) {
      openEscBtn.addEventListener("click", function () {
        if (!appState.selectedCase) {
          showToast("请先选择一个工单", true);
          return;
        }
        escModal.classList.remove("hidden");
      });
    }
    [closeEscBtn, cancelEscBtn].forEach(function (b) {
      if (b && escModal) {
        b.addEventListener("click", function () {
          escModal.classList.add("hidden");
        });
      }
    });
    if (escForm && escModal) {
      escForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        if (!appState.selectedCase) return;
        try {
          const res = await apiRequest("/api/drafts/escalation", {
            method: "POST",
            jsonBody: {
              case_name: appState.selectedCase.name,
              reason: document.getElementById("escalate-reason-input").value,
              justification: document.getElementById("escalate-just-input").value,
            },
          });
          escModal.classList.add("hidden");
          escForm.reset();
          if (res.draft) {
            appState.pendingDrafts.unshift(res.draft);
            renderPendingDraftCards();
            appendChatBubble(
              "assistant",
              "🛡️ 已生成工单升级确认草稿 " + res.draft.draft_id + "，请在确认卡片中点击「确认升级工单」。",
              [{ tool: "prepare_escalation_draft" }]
            );
          }
        } catch (err) {
          showToast(err.message, true);
        }
      });
    }

    // Close Case Modal
    const closeModal = document.getElementById("modal-close-confirm");
    const openCloseBtn = document.getElementById("btn-close-case");
    const dismissCloseBtn = document.getElementById("btn-dismiss-close-modal");
    const cancelCloseBtn = document.getElementById("btn-cancel-close-case");
    const confirmCloseBtn = document.getElementById("btn-confirm-close-case");

    if (openCloseBtn && closeModal) {
      openCloseBtn.addEventListener("click", function () {
        if (!appState.selectedCase) return;
        closeModal.classList.remove("hidden");
      });
    }
    [dismissCloseBtn, cancelCloseBtn].forEach(function (b) {
      if (b && closeModal) {
        b.addEventListener("click", function () {
          closeModal.classList.add("hidden");
        });
      }
    });
    if (confirmCloseBtn && closeModal) {
      confirmCloseBtn.addEventListener("click", async function () {
        if (!appState.selectedCase) return;
        const c = appState.selectedCase;
        try {
          const res = await apiRequest(
            "/api/cases/" + encodeURIComponent(c.project_id) + "/" + encodeURIComponent(c.case_id) + "/close",
            { method: "POST" }
          );
          closeModal.classList.add("hidden");
          appState.selectedCase = res.case;
          renderSelectedCaseDetail();
          await loadCasesForActiveProject();
          showToast("工单已关闭", false);
        } catch (err) {
          showToast(err.message, true);
        }
      });
    }

    // Comment Form
    const commentForm = document.getElementById("comment-form");
    const commentInput = document.getElementById("comment-input");
    if (commentForm && commentInput) {
      commentForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        if (!appState.selectedCase) return;
        const c = appState.selectedCase;
        try {
          const res = await apiRequest(
            "/api/cases/" + encodeURIComponent(c.project_id) + "/" + encodeURIComponent(c.case_id) + "/comments",
            {
              method: "POST",
              jsonBody: { body: commentInput.value },
            }
          );
          commentInput.value = "";
          appState.selectedCase = res.case;
          renderSelectedCaseDetail();
          showToast("已追加评论", false);
        } catch (err) {
          showToast(err.message, true);
        }
      });
    }

    // Attachment Upload Form
    const uploadForm = document.getElementById("attachment-upload-form");
    const fileInput = document.getElementById("attachment-file-input");
    if (uploadForm && fileInput) {
      uploadForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        if (!appState.selectedCase || !fileInput.files || fileInput.files.length === 0) {
          return;
        }
        const c = appState.selectedCase;
        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        try {
          const res = await apiRequest(
            "/api/cases/" + encodeURIComponent(c.project_id) + "/" + encodeURIComponent(c.case_id) + "/attachments",
            {
              method: "POST",
              formData: formData,
            }
          );
          fileInput.value = "";
          appState.selectedCase = res.case;
          renderSelectedCaseDetail();
          showToast("诊断附件上传成功", false);
        } catch (err) {
          showToast(err.message, true);
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindEvents();
    initializePortal();
  });
})();
