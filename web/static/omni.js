/* Omni client behavior. Small on purpose: the pages are server rendered and
   most actions are plain form posts. What lives here:
     - landing: example chips type their prompt into the box
     - inbox: the composer prompt line writes the draft into the reply box,
       compose/detail tabs, provenance quotes, auto-submit selects,
       proposal cards (confirm / cancel), live search
     - top bar: the setup prompt line proposes a change, then applies it
     - connections: sample buttons fill the paste box */
(function () {
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).getAttribute
    ? document.querySelector('meta[name="csrf-token"]').getAttribute('content') : '';

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
                'X-CSRF-Token': csrf, 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (d) { if (!r.ok) throw d; return d; }); });
  }

  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // ---- Landing: example chips ------------------------------------------
  var prompt = document.getElementById('omPrompt');
  var exampleField = document.getElementById('omExample');
  var startForm = document.getElementById('omStartForm');
  if (prompt && startForm) {
    document.querySelectorAll('.om-examples [data-example]').forEach(function (chip) {
      chip.addEventListener('click', function () {
        var key = chip.getAttribute('data-example');
        var text = chip.getAttribute('data-prompt') || '';
        if (exampleField.value === key && prompt.value.trim() === text.trim()) {
          startForm.requestSubmit ? startForm.requestSubmit() : startForm.submit();
          return;
        }
        exampleField.value = key;
        document.querySelectorAll('.om-examples [data-example]').forEach(function (c) {
          c.classList.toggle('is-active', c === chip);
        });
        var write = window.Bridget ? window.Bridget.fill(prompt, text, {maxMs: 700}) : Promise.resolve(prompt.value = text);
        write.then(function () { prompt.focus(); });
      });
    });
    prompt.addEventListener('input', function () {
      // Typing your own words means it is no longer the example.
      var chip = document.querySelector('.om-examples [data-example].is-active');
      if (chip && prompt.value.trim() !== (chip.getAttribute('data-prompt') || '').trim()) {
        exampleField.value = '';
        chip.classList.remove('is-active');
      }
    });
    prompt.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (prompt.value.trim() || exampleField.value) {
          startForm.requestSubmit ? startForm.requestSubmit() : startForm.submit();
        }
      }
    });
    document.querySelectorAll('[data-focus-prompt]').forEach(function (a) {
      a.addEventListener('click', function () { setTimeout(function () { prompt.focus(); }, 300); });
    });
  }

  // ---- Inbox: composer prompt line --------------------------------------
  var askBox = document.querySelector('.mh-ask[data-draft-url]');
  var askInput = document.querySelector('[data-ask-input]');
  var askGo = document.querySelector('[data-ask-go]');
  var askSuggest = document.querySelector('[data-ask-suggest]');
  var askStatus = document.querySelector('[data-ask-status]');
  var replyArea = document.getElementById('replyBody');
  var policyLine = document.querySelector('[data-policy-line]');

  function syncSuggest() {
    if (!askSuggest) return;
    askSuggest.hidden = !!(replyArea && replyArea.value.trim());
  }
  function setAskStatus(text, kind) {
    if (!askStatus) return;
    askStatus.textContent = text || '';
    askStatus.hidden = !text;
    askStatus.classList.toggle('is-error', kind === 'error');
  }
  function askOmni(instruction) {
    if (!askBox || askBox.classList.contains('is-busy')) return;
    var name = (askBox.querySelector('.mh-ask-name') || {}).textContent || 'Omni';
    askBox.classList.add('is-busy');
    if (askGo) askGo.disabled = true;
    setAskStatus(name + ' is writing...');
    post(askBox.getAttribute('data-draft-url'), {instruction: instruction || ''})
      .then(function (data) {
        if (askInput) askInput.value = '';
        setAskStatus('');
        if (policyLine && data.policy) {
          policyLine.innerHTML = policyLine.innerHTML.replace(/[^<]*$/, '') +
            (data.policy === 'review'
              ? ' ' + name + ' read this as ' + data.category_words + '. Nothing sends without you.'
              : ' Routine reply. You still press Send.');
        }
        var written = window.Bridget ? window.Bridget.fill(replyArea, data.draft)
                                     : Promise.resolve(replyArea && (replyArea.value = data.draft));
        return written.then(function () {
          syncSuggest();
          if (replyArea) {
            replyArea.focus();
            replyArea.setSelectionRange(replyArea.value.length, replyArea.value.length);
          }
        });
      })
      .catch(function (err) {
        askBox.classList.add('is-error');
        setAskStatus((err && (err.message || err.error)) || 'Could not write that one. Try again, or type the reply yourself.', 'error');
        setTimeout(function () { askBox.classList.remove('is-error'); }, 2400);
        setTimeout(function () { if (askStatus && askStatus.classList.contains('is-error')) setAskStatus(''); }, 7000);
      })
      .finally(function () {
        askBox.classList.remove('is-busy');
        if (askGo) askGo.disabled = false;
      });
  }
  if (askInput) {
    askInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); askOmni(askInput.value.trim()); }
      else if (e.key === 'Escape' && askInput.value) { e.preventDefault(); askInput.value = ''; }
    });
  }
  if (askGo) askGo.addEventListener('click', function () { askOmni(askInput ? askInput.value.trim() : ''); });
  document.addEventListener('click', function (e) {
    var chip = e.target.closest('[data-ask-fill]');
    if (!chip || !askBox) return;
    var text = chip.getAttribute('data-ask-fill');
    if (askInput) askInput.value = text;
    // From the side panel on a phone, jump back to the conversation first.
    var convTab = document.querySelector('[data-detail-tab="conversation"]');
    if (convTab && chip.closest('[data-detail-panel="applicant"]')) convTab.click();
    askOmni(text);
  });
  if (replyArea) {
    replyArea.addEventListener('input', syncSuggest);
    replyArea.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        var f = replyArea.closest('form');
        if (f) { e.preventDefault(); f.requestSubmit ? f.requestSubmit() : f.submit(); }
      }
    });
    syncSuggest();
  }

  // ---- Inbox: tabs, selects, provenance -----------------------------------
  document.querySelectorAll('[data-compose-tab]').forEach(function (tab) {
    tab.addEventListener('click', function () {
      var which = tab.getAttribute('data-compose-tab');
      document.querySelectorAll('[data-compose-tab]').forEach(function (t) {
        var on = t === tab;
        t.classList.toggle('is-active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      document.querySelectorAll('[data-compose-panel]').forEach(function (p) {
        p.hidden = p.getAttribute('data-compose-panel') !== which;
      });
      var ta = document.querySelector('[data-compose-panel="' + which + '"] textarea:not([data-ask-input])');
      if (ta) ta.focus();
    });
  });
  document.querySelectorAll('[data-detail-tab]').forEach(function (tab) {
    tab.addEventListener('click', function () {
      var which = tab.getAttribute('data-detail-tab');
      document.querySelectorAll('[data-detail-tab]').forEach(function (t) {
        var on = t === tab;
        t.classList.toggle('is-active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      document.querySelectorAll('[data-detail-panel]').forEach(function (p) {
        p.classList.toggle('is-active', p.getAttribute('data-detail-panel') === which);
      });
    });
  });
  document.querySelectorAll('select[data-autosubmit]').forEach(function (sel) {
    sel.addEventListener('change', function () {
      var f = sel.closest('form');
      if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); }
    });
  });
  document.addEventListener('click', function (e) {
    var src = e.target.closest('[data-om-src]');
    if (src) {
      var q = src.parentNode.querySelector('.om-quote');
      if (!q) return;
      q.hidden = !q.hidden;
      src.setAttribute('aria-expanded', q.hidden ? 'false' : 'true');
      src.textContent = q.hidden ? 'Where' : 'Hide';
      return;
    }
    var quote = e.target.closest('.om-quote');
    if (quote) {
      var id = quote.getAttribute('data-message-id');
      var msg = id && document.querySelector('[data-message-id="' + id + '"]');
      if (!msg) return;
      var convTab = document.querySelector('[data-detail-tab="conversation"]');
      if (convTab && window.innerWidth <= 780) convTab.click();
      msg.scrollIntoView({behavior: reduced() ? 'auto' : 'smooth', block: 'center'});
      msg.classList.add('is-cited');
      setTimeout(function () { msg.classList.remove('is-cited'); }, 1400);
    }
  });

  // ---- Inbox: proposal cards ---------------------------------------------
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.om-proposal [data-act]');
    if (!btn) return;
    var card = btn.closest('.om-proposal');
    var token = card.getAttribute('data-action-token');
    var ws = document.querySelector('[data-wid]');
    var base = '/omni/w/' + (ws ? ws.getAttribute('data-wid') : '') + '/act';
    var act = btn.getAttribute('data-act');
    var text = (card.querySelector('.cc-text') || {}).value;
    btn.disabled = true;
    post(base, {token: token, action: act, text: text}).then(function (d) {
      if (act === 'cancel' || !d.message) { card.remove(); return; }
      card.classList.add('is-done');
      card.innerHTML = '<div class="cc-done">' + (d.answer || 'Done.') + '</div>';
      setTimeout(function () { window.location.reload(); }, 600);
    }).catch(function (err) {
      btn.disabled = false;
      var note = card.querySelector('.cc-note');
      if (note) note.textContent = (err && (err.message || err.error)) || 'Could not do that.';
    });
  });

  // ---- Inbox: live search over rendered rows -------------------------------
  var live = document.querySelector('[data-live-search]');
  var stack = document.querySelector('[data-thread-stack]');
  if (live && stack) {
    var timer;
    live.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = live.value.trim().toLowerCase();
        var any = false;
        stack.querySelectorAll('.mh-thread-row').forEach(function (row) {
          var hit = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
          row.classList.toggle('is-filtered', !hit);
          row.style.display = hit ? '' : 'none';
          if (hit) any = true;
        });
        var empty = stack.querySelector('[data-live-empty]');
        if (empty) empty.hidden = any || !q;
      }, 50);
    });
  }

  // ---- Top bar: setup changes by prompt -------------------------------------
  var changeForm = document.querySelector('form[data-change-url]');
  var changeInput = document.querySelector('[data-change-input]');
  var changeStatus = document.querySelector('[data-change-status]');
  var dock = document.querySelector('[data-proposal-dock]');
  function setChangeStatus(text, kind) {
    if (!changeStatus) return;
    changeStatus.textContent = text || '';
    changeStatus.hidden = !text;
    changeStatus.classList.toggle('is-error', kind === 'error');
  }
  function renderChange(p) {
    if (!dock) return;
    dock.hidden = false;
    var lines = (p.diff || []).map(function (l) { return '<li>' + escapeHtml(l) + '</li>'; }).join('');
    var refused = (p.refused || []).map(function (l) { return '<li class="is-refused">' + escapeHtml(l) + '</li>'; }).join('');
    dock.innerHTML =
      '<div class="copilot-confirm om-change" data-change-token="' + (p.token || '') + '">' +
      '<div class="cc-to">' + escapeHtml(p.title || 'Change to your setup') + '</div>' +
      (lines || refused ? '<ul class="om-diff">' + lines + refused + '</ul>' : '') +
      (p.after ? '<div class="cc-note">' + escapeHtml(p.after) + '</div>' : '') +
      '<div class="cc-actions">' +
      (p.token ? '<button type="button" class="btn btn-primary btn-sm" data-change-act="apply">Apply</button>' : '') +
      '<button type="button" class="btn btn-ghost btn-sm" data-change-act="dismiss">' + (p.token ? 'Cancel' : 'OK') + '</button>' +
      '</div></div>';
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }
  if (changeForm && changeInput) {
    changeForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var text = changeInput.value.trim();
      if (!text) return;
      setChangeStatus('Working out the change...');
      changeForm.classList.add('is-busy');
      post(changeForm.getAttribute('data-change-url'), {instruction: text}).then(function (d) {
        setChangeStatus('');
        changeInput.value = '';
        renderChange(d.proposal || d);
      }).catch(function (err) {
        setChangeStatus((err && (err.message || err.error)) || 'Could not work that out. Try different words.', 'error');
        setTimeout(function () { if (changeStatus && changeStatus.classList.contains('is-error')) setChangeStatus(''); }, 8000);
      }).finally(function () { changeForm.classList.remove('is-busy'); });
    });
    document.addEventListener('click', function (e) {
      var b = e.target.closest('[data-change-act]');
      if (!b || !dock) return;
      var card = b.closest('[data-change-token]');
      var token = card ? card.getAttribute('data-change-token') : '';
      if (b.getAttribute('data-change-act') === 'dismiss' || !token) {
        if (token) post(changeForm.getAttribute('data-change-url') + '/cancel', {token: token}).catch(function () {});
        dock.hidden = true; dock.innerHTML = '';
        return;
      }
      b.disabled = true;
      post(changeForm.getAttribute('data-change-url') + '/apply', {token: token}).then(function (d) {
        window.location.assign(d.redirect || window.location.href);
      }).catch(function (err) {
        b.disabled = false;
        var note = card.querySelector('.cc-note');
        var msg = (err && (err.message || err.error)) || 'Could not apply that.';
        if (note) note.textContent = msg; else card.insertAdjacentHTML('beforeend', '<div class="cc-note">' + escapeHtml(msg) + '</div>');
      });
    });
  }

  // ---- Top bar: agent actions from the side panel ----------------------------
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-agent-action]');
    if (!b) return;
    var ws = document.querySelector('[data-wid]');
    var active = document.querySelector('.mh-thread-row.is-active');
    var cid = active ? active.getAttribute('data-thread-id') : (new URLSearchParams(location.search)).get('thread');
    if (!ws || !cid) return;
    b.disabled = true;
    post('/omni/w/' + ws.getAttribute('data-wid') + '/agent', {action: b.getAttribute('data-agent-action'), thread_id: parseInt(cid, 10)})
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        b.disabled = false;
        var hint = b.parentNode.querySelector('.om-hint');
        if (hint) hint.textContent = (err && (err.message || err.error)) || 'Could not propose that.';
      });
  });

  // ---- Share ----------------------------------------------------------------
  document.querySelectorAll('.om-share[data-share-url]').forEach(function (b) {
    b.addEventListener('click', function () {
      var url = b.getAttribute('data-share-url');
      var done = function () { b.textContent = 'Link copied'; setTimeout(function () { b.textContent = 'Share'; }, 1800); };
      if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, function () { window.prompt('Copy this link', url); });
      else window.prompt('Copy this link', url);
    });
  });

  // ---- Connections: sample inputs --------------------------------------------
  var pasteText = document.getElementById('omPasteText');
  var pasteSource = document.getElementById('omPasteSource');
  if (pasteText) {
    document.querySelectorAll('[data-sample-text]').forEach(function (b) {
      b.addEventListener('click', function () {
        var src = b.getAttribute('data-sample-source');
        if (pasteSource && src) {
          var opt = Array.prototype.find.call(pasteSource.options, function (o) { return o.value === src; });
          if (opt) pasteSource.value = src;
        }
        var write = window.Bridget ? window.Bridget.fill(pasteText, b.getAttribute('data-sample-text'), {maxMs: 900})
                                   : Promise.resolve(pasteText.value = b.getAttribute('data-sample-text'));
        write.then(function () { pasteText.focus(); });
      });
    });
  }
})();

/* ---- Builder: the interview drives the preview ------------------------------ */
(function () {
  var root = document.querySelector('.om-build');
  if (!root) return;
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {getAttribute: function () { return ''; }}).getAttribute('content');
  var log = document.getElementById('omLog');
  var input = document.getElementById('omChatInput');
  var form = document.getElementById('omChatAsk');
  var status = document.querySelector('[data-chat-status]');
  var previewHost = document.querySelector('[data-preview]');
  var openBtn = document.querySelector('[data-open-inbox]');
  var busy = false;

  function post(url, body) {
    return fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': csrf, 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(body || {})})
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw d; return d; }); });
  }
  function reduced() { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  function setStatus(t, kind) { if (!status) return; status.textContent = t || ''; status.hidden = !t; status.classList.toggle('is-error', kind === 'error'); }
  function scrollLog() { if (log) log.scrollTop = log.scrollHeight; }
  function currentQuestion() { var qs = log.querySelectorAll('.om-msg.is-question'); return qs.length ? qs[qs.length - 1] : null; }

  function append(html) {
    if (!html) return null;
    var tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    var nodes = Array.prototype.slice.call(tpl.content.childNodes);
    nodes.forEach(function (n) { log.appendChild(n); });
    scrollLog();
    return nodes.filter(function (n) { return n.nodeType === 1; });
  }
  function reveal(nodes) {
    // Type the agent's line, then show the controls.
    var chain = Promise.resolve();
    (nodes || []).forEach(function (n) {
      var p = n.querySelector && n.querySelector('[data-stream]');
      if (!p) return;
      var text = p.textContent;
      var controls = n.querySelectorAll('[data-options], .om-q-actions, .om-summary, .om-why');
      controls.forEach(function (c) { c.style.visibility = 'hidden'; });
      chain = chain.then(function () {
        return (window.Bridget && !reduced()) ? window.Bridget.stream(p, text, {maxMs: 900}) : Promise.resolve(p.textContent = text);
      }).then(function () { controls.forEach(function (c) { c.style.visibility = ''; }); scrollLog(); });
    });
    return chain;
  }
  function applyState(d) {
    if (d.you_html) append(d.you_html);
    var nodes = append(d.agent_html);
    if (previewHost && d.preview_html) {
      previewHost.innerHTML = d.preview_html;
      var frame = previewHost.querySelector('[data-preview-frame]');
      if (frame) { frame.classList.add('is-updating'); setTimeout(function () { frame.classList.remove('is-updating'); }, 600); }
    }
    if (d.progress) {
      Object.keys(d.progress).forEach(function (k) {
        var li = document.querySelector('[data-progress] [data-step="' + k + '"]');
        if (li) li.classList.toggle('is-done', !!d.progress[k]);
      });
      var n = Object.keys(d.progress).filter(function (k) { return d.progress[k]; }).length;
      var compact = document.querySelector('[data-progress-compact]');
      if (compact) compact.textContent = n + ' of 5 decided';
    }
    if (input) {
      var q = d.question;
      input.placeholder = !q ? 'Tell me what to change, or press Build my inbox' : (q.kind === 'text' ? 'Type an answer' : (q.multi ? 'Pick above, or type your own' : 'Or type your own'));
    }
    return reveal(nodes);
  }
  function send(url, body) {
    if (busy) return Promise.resolve();
    busy = true; root.classList.add('is-busy');
    setStatus('');
    return post(url, body).then(applyState).catch(function (err) {
      setStatus((err && (err.message || err.error)) || 'Could not reach the builder. Try again.', 'error');
    }).finally(function () { busy = false; root.classList.remove('is-busy'); });
  }
  function answerFrom(qEl, typed) {
    var qid = qEl.getAttribute('data-qid');
    var kind = qEl.getAttribute('data-kind');
    var multi = qEl.getAttribute('data-multi') === '1';
    var value;
    if (kind === 'tiles') {
      value = Array.prototype.map.call(qEl.querySelectorAll('input:checked'), function (i) { return i.value; });
      if (!multi) value = value[0] || '';
    } else if (kind === 'chips' || kind === 'toggle') {
      value = Array.prototype.map.call(qEl.querySelectorAll('.mh-quick-chip.is-active'), function (b) { return b.getAttribute('data-value'); });
      if (typed) value = value.concat(typed.split(',').map(function (s) { return s.trim(); }).filter(Boolean));
      if (!multi) value = value[0] || '';
    } else {
      value = typed || (qEl.querySelector('[data-default]') || {getAttribute: function () { return ''; }}).getAttribute('data-default') || '';
    }
    return send(root.getAttribute('data-answer-url'), {qid: qid, value: value});
  }

  log.addEventListener('click', function (e) {
    var chip = e.target.closest('.om-chips .mh-quick-chip');
    if (chip) {
      var qEl = chip.closest('.om-msg.is-question');
      if (qEl !== currentQuestion()) return;
      var multi = qEl.getAttribute('data-multi') === '1';
      if (multi) {
        chip.classList.toggle('is-active');
        chip.setAttribute('aria-pressed', chip.classList.contains('is-active') ? 'true' : 'false');
      } else {
        qEl.querySelectorAll('.mh-quick-chip').forEach(function (b) { b.classList.toggle('is-active', b === chip); });
        answerFrom(qEl, '');
      }
      return;
    }
    var cont = e.target.closest('[data-continue]');
    if (cont) { var qEl2 = cont.closest('.om-msg.is-question'); if (qEl2) answerFrom(qEl2, ''); return; }
    var build = e.target.closest('[data-build]');
    if (build) {
      build.disabled = true; build.textContent = 'Building...';
      post(root.getAttribute('data-build-url'), {}).then(function (d) {
        window.location.assign(d.redirect);
      }).catch(function (err) { build.disabled = false; build.textContent = 'Build my inbox'; setStatus((err && (err.message || err.error)) || 'Could not build.', 'error'); });
    }
  });
  log.addEventListener('change', function (e) {
    var tile = e.target.closest('.om-tiles input[type=radio]');
    if (tile) { var qEl = tile.closest('.om-msg.is-question'); if (qEl === currentQuestion()) answerFrom(qEl, ''); }
  });
  if (form) form.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = (input.value || '').trim();
    if (!text) return;
    input.value = '';
    var qEl = currentQuestion();
    var answered = qEl && Array.prototype.some.call(log.querySelectorAll('.om-msg.is-you'), function (y) { return y.compareDocumentPosition(qEl) & Node.DOCUMENT_POSITION_PRECEDING; }) && false;
    if (qEl && !qEl.classList.contains('is-answered') && !document.querySelector('.om-msg.is-summary')) {
      qEl.classList.add('is-answered');
      answerFrom(qEl, text);
    } else {
      send(root.getAttribute('data-change-url'), {instruction: text});
    }
  });
  var skip = document.querySelector('[data-skip]');
  if (skip) skip.addEventListener('click', function () { send(root.getAttribute('data-skip-url'), {}); });
  document.querySelectorAll('[data-build-tab]').forEach(function (tab) {
    tab.addEventListener('click', function () {
      var which = tab.getAttribute('data-build-tab');
      document.querySelectorAll('[data-build-tab]').forEach(function (t) { t.classList.toggle('is-active', t === tab); });
      document.querySelectorAll('[data-build-panel]').forEach(function (p) { p.classList.toggle('is-active', p.getAttribute('data-build-panel') === which); });
    });
  });
  // Mark questions already answered on a refreshed page.
  var qs = log.querySelectorAll('.om-msg.is-question');
  for (var i = 0; i < qs.length - 1; i++) qs[i].classList.add('is-answered');
  scrollLog();
  var last = currentQuestion();
  if (last && !last.classList.contains('is-answered')) reveal([last]);
})();
