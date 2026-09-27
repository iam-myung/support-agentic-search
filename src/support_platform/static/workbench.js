/**
 * Phase-2 pilot workbench client (S15).
 * Wires OP-12 login, OP-07 create, OP-09 SSE (+ Last-Event-ID), OP-08 reconcile,
 * OP-10/11 HITL. ANSWER_CHUNK is rendered only from named SSE events (validated server-side).
 */
(function () {
  "use strict";

  var CSRF_HEADER = "X-CSRF-Token";
  var csrfToken = null;
  var sessionUser = null;
  var activeInvestigationId = null;
  var eventSource = null;
  var lastEventId = 0;

  function $(id) {
    return document.getElementById(id);
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "idem-" + String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  }

  function setLoginError(msg) {
    var el = $("login-error");
    if (!el) return;
    if (msg) {
      el.textContent = msg;
      el.hidden = false;
    } else {
      el.textContent = "";
      el.hidden = true;
    }
  }

  function setHitlError(msg) {
    var el = $("hitl-error");
    if (!el) return;
    if (msg) {
      el.textContent = msg;
      el.hidden = false;
    } else {
      el.textContent = "";
      el.hidden = true;
    }
  }

  function updateSessionUi() {
    var form = $("login-form");
    var bar = $("session-bar");
    var label = $("session-label");
    if (sessionUser && csrfToken) {
      if (form) form.hidden = true;
      if (bar) bar.hidden = false;
      if (label) label.textContent = "已登录 · " + (sessionUser.role || "");
    } else {
      if (form) form.hidden = false;
      if (bar) bar.hidden = true;
      if (label) label.textContent = "";
    }
  }

  function eventsUrl(investigationId, afterId) {
    var base = "/api/v2/investigations/" + encodeURIComponent(investigationId) + "/events";
    if (afterId && afterId > 0) {
      return base + "?last_event_id=" + encodeURIComponent(String(afterId));
    }
    return base;
  }

  function appendProgressItem(text) {
    var list = $("progress-events");
    if (!list) return;
    var li = document.createElement("li");
    li.textContent = text;
    list.appendChild(li);
  }

  function showProgress(investigationId) {
    var panel = $("progress-panel");
    var idEl = $("progress-id");
    if (panel) panel.hidden = false;
    if (idEl) idEl.textContent = investigationId;
  }

  function showHitl(payload) {
    var panel = $("hitl-panel");
    if (!panel) return;
    panel.hidden = false;
    var reason = $("hitl-reason");
    var iid = $("hitl-interrupt-id");
    if (reason) {
      reason.textContent =
        (payload && (payload.visible_question || payload.summary || payload.reason)) ||
        "REVIEW_REQUIRED / INTERRUPTED — 请人工决定后继续";
    }
    if (iid) {
      iid.value = (payload && (payload.interrupt_id || payload.interruptId)) || "";
    }
  }

  function hideHitl() {
    var panel = $("hitl-panel");
    if (panel) panel.hidden = true;
    setHitlError("");
  }

  /**
   * Render only validated ANSWER_CHUNK payloads from SSE.
   * Do not invent client-side answer streaming before server validation.
   */
  function handleAnswerChunk(payload) {
    var stream = $("answer-stream");
    if (!stream || !payload) return;
    var chunk = payload.chunk || payload.text || "";
    if (!chunk) return;
    stream.textContent = (stream.textContent || "") + chunk;
  }

  function handleSseEvent(type, data, eventId) {
    var seq = eventId ? parseInt(eventId, 10) : NaN;
    if (!isNaN(seq) && seq > lastEventId) {
      lastEventId = seq;
    }

    var statusEl = $("progress-task-status");
    var payload = data || {};

    if (type === "QUEUED" || type === "STARTED" || type === "STEP") {
      if (statusEl) statusEl.textContent = type;
      // Phase3: prefer public_payload.label (same as public_steps[].label).
      var stepLabel =
        (payload.label || payload.summary || payload.kind || type) +
        (payload.authorized_source_count != null
          ? " · sources " + payload.authorized_source_count
          : "");
      appendProgressItem(stepLabel);
    } else if (type === "REVIEW_REQUIRED") {
      if (statusEl) statusEl.textContent = "INTERRUPTED / REVIEW_REQUIRED";
      appendProgressItem("REVIEW_REQUIRED");
      showHitl(payload);
    } else if (type === "ANSWER_CHUNK") {
      // Named SSE event only — server already validated claim/evidence.
      handleAnswerChunk(payload);
      appendProgressItem("ANSWER_CHUNK");
    } else if (type === "COMPLETED" || type === "FAILED" || type === "CANCELLED") {
      if (statusEl) statusEl.textContent = type;
      appendProgressItem(type);
      if (type !== "COMPLETED") {
        hideHitl();
      }
      reconcileWithOp08(activeInvestigationId);
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
    }
  }

  function parseSseData(raw) {
    if (!raw) return {};
    try {
      return JSON.parse(raw);
    } catch (e) {
      return {};
    }
  }

  function subscribeEvents(investigationId) {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    // Browser EventSource sends Last-Event-ID automatically on reconnect.
    // Query fallback for first connect after a known sequence.
    var url = eventsUrl(investigationId, lastEventId > 0 ? lastEventId : 0);
    eventSource = new EventSource(url, { withCredentials: true });

    var types = [
      "QUEUED",
      "STARTED",
      "STEP",
      "REVIEW_REQUIRED",
      "ANSWER_CHUNK",
      "COMPLETED",
      "FAILED",
      "CANCELLED",
      "message",
    ];
    types.forEach(function (t) {
      eventSource.addEventListener(t, function (ev) {
        var typ = t === "message" ? (ev.type || "message") : t;
        var data = parseSseData(ev.data);
        if (data.type || data.event_type) {
          typ = data.type || data.event_type;
        }
        handleSseEvent(typ, data.public_payload || data, ev.lastEventId || data.sequence);
      });
    });

    eventSource.onerror = function () {
      // Native reconnect will attach Last-Event-ID; stale IDs prompt OP-08 re-read server-side.
      appendProgressItem("SSE 连接中断，正在重连（Last-Event-ID）…");
    };
  }

  function reconcileWithOp08(investigationId) {
    if (!investigationId) return;
    fetch("/api/v2/investigations/" + encodeURIComponent(investigationId), {
      credentials: "same-origin",
    })
      .then(function (r) {
        if (r.status === 401) {
          sessionUser = null;
          csrfToken = null;
          updateSessionUi();
          return null;
        }
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (detail) {
        if (!detail) return;
        var statusEl = $("progress-task-status");
        if (statusEl) {
          statusEl.textContent =
            (detail.task_status || "") +
            (detail.result_status ? " / " + detail.result_status : "");
        }
        if (detail.task_status === "INTERRUPTED") {
          showHitl({
            interrupt_id: detail.interrupt_id,
            summary: detail.result_status || "INTERRUPTED",
          });
        }
        if (detail.task_status === "COMPLETED" && detail.output && detail.output.summary) {
          var stream = $("answer-stream");
          if (stream && !stream.textContent) {
            stream.textContent = detail.output.summary;
          }
        }
      })
      .catch(function () {});
  }

  function login(username, password) {
    setLoginError("");
    return fetch("/api/v2/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username, password: password }),
    }).then(function (r) {
      if (r.status === 401) {
        setLoginError("用户名或密码错误");
        return null;
      }
      if (!r.ok) {
        setLoginError("登录失败（" + r.status + "）");
        return null;
      }
      return r.json();
    }).then(function (body) {
      if (!body) return;
      csrfToken = body.csrf_token;
      sessionUser = { user_id: body.user_id, role: body.role };
      updateSessionUi();
    });
  }

  function authHeaders(extra) {
    var headers = extra ? Object.assign({}, extra) : {};
    if (csrfToken) headers[CSRF_HEADER] = csrfToken;
    return headers;
  }

  function logout() {
    if (!csrfToken) return;
    fetch("/api/v2/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: authHeaders(),
    }).finally(function () {
      csrfToken = null;
      sessionUser = null;
      updateSessionUi();
    });
  }

  function createInvestigationV2() {
    if (!csrfToken) {
      setLoginError("请先登录后再使用 v2 进度提交");
      return;
    }
    var q = ($("question") && $("question").value) || "";
    q = q.trim();
    if (!q) return;

    var context = {};
    var ec = $("error_code");
    var pv = $("product_version");
    if (ec && ec.value.trim()) context.error_code = ec.value.trim();
    if (pv && pv.value.trim()) context.product_version = pv.value.trim();

    var btn = $("v2-submit-btn");
    if (btn) {
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
    }

    lastEventId = 0;
    var list = $("progress-events");
    if (list) list.innerHTML = "";
    var stream = $("answer-stream");
    if (stream) stream.textContent = "";
    hideHitl();

    fetch("/api/v2/investigations", {
      method: "POST",
      credentials: "same-origin",
      headers: authHeaders({
        "Content-Type": "application/json",
        "Idempotency-Key": uuid(),
      }),
      body: JSON.stringify({ question: q, context: context }),
    })
      .then(function (r) {
        if (r.status === 401) {
          setLoginError("会话已失效，请重新登录");
          sessionUser = null;
          csrfToken = null;
          updateSessionUi();
          return null;
        }
        if (!r.ok) {
          appendProgressItem("创建失败 " + r.status);
          return null;
        }
        return r.json();
      })
      .then(function (body) {
        if (!body) return;
        activeInvestigationId = body.id;
        showProgress(body.id);
        var statusEl = $("progress-task-status");
        if (statusEl) statusEl.textContent = body.task_status || "QUEUED";
        subscribeEvents(body.id);
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
          btn.removeAttribute("aria-busy");
        }
      });
  }

  function resumeHitl(action) {
    if (!activeInvestigationId || !csrfToken) {
      setHitlError("需要已登录会话与调查 ID");
      return;
    }
    var interruptId = ($("hitl-interrupt-id") && $("hitl-interrupt-id").value) || "";
    if (!interruptId) {
      setHitlError("缺少 interrupt_id");
      return;
    }
    var inputText = ($("hitl-input") && $("hitl-input").value) || "";
    setHitlError("");

    fetch(
      "/api/v2/investigations/" + encodeURIComponent(activeInvestigationId) + "/resume",
      {
        method: "POST",
        credentials: "same-origin",
        headers: authHeaders({
          "Content-Type": "application/json",
          "Idempotency-Key": uuid(),
        }),
        body: JSON.stringify({
          interrupt_id: interruptId,
          action: action,
          input: inputText ? { text: inputText } : {},
        }),
      }
    ).then(function (r) {
      if (r.status === 409) {
        setHitlError("冲突：非 INTERRUPTED 或重复动作");
        return;
      }
      if (!r.ok) {
        setHitlError("恢复失败（" + r.status + "）");
        return;
      }
      if (action === "REJECT") {
        appendProgressItem("REJECT — 不记为执行成功");
        hideHitl();
      } else {
        hideHitl();
        appendProgressItem(action + " 已提交");
      }
      // Re-subscribe / continue; Last-Event-ID catch-up from lastEventId.
      subscribeEvents(activeInvestigationId);
    });
  }

  function cancelInvestigation() {
    if (!activeInvestigationId || !csrfToken) {
      setHitlError("需要已登录会话与调查 ID");
      return;
    }
    fetch(
      "/api/v2/investigations/" + encodeURIComponent(activeInvestigationId) + "/cancel",
      {
        method: "POST",
        credentials: "same-origin",
        headers: authHeaders({
          "Content-Type": "application/json",
          "Idempotency-Key": uuid(),
        }),
        body: JSON.stringify({}),
      }
    ).then(function (r) {
      if (!r.ok) {
        setHitlError("取消失败（" + r.status + "）");
        return;
      }
      appendProgressItem("CANCEL 已请求");
      hideHitl();
      reconcileWithOp08(activeInvestigationId);
    });
  }

  function bindLegacyForm() {
    var form = $("investigate-form");
    if (form) {
      form.addEventListener("submit", function () {
        var btn = $("submit-btn");
        if (btn) {
          btn.disabled = true;
          btn.setAttribute("aria-busy", "true");
          btn.textContent = "提交中…";
        }
      });
    }
    var copyBtn = $("copy-suggestion");
    if (copyBtn && navigator.clipboard) {
      copyBtn.addEventListener("click", function () {
        var el = $(copyBtn.getAttribute("data-copy-target"));
        if (el) navigator.clipboard.writeText(el.textContent.trim());
      });
    }
  }

  function init() {
    bindLegacyForm();
    updateSessionUi();

    var loginForm = $("login-form");
    if (loginForm) {
      loginForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var u = ($("username") && $("username").value) || "";
        var p = ($("password") && $("password").value) || "";
        login(u.trim(), p);
      });
    }

    var logoutBtn = $("logout-btn");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", function () {
        logout();
      });
    }

    var v2Btn = $("v2-submit-btn");
    if (v2Btn) {
      v2Btn.addEventListener("click", function () {
        createInvestigationV2();
      });
    }

    document.querySelectorAll("[data-hitl-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        resumeHitl(btn.getAttribute("data-hitl-action"));
      });
    });

    var cancelBtn = document.querySelector("[data-hitl-cancel]");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        cancelInvestigation();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
