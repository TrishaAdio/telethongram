/* api.js — Telethongram frontend data boundary (production).
   ─────────────────────────────────────────────────────────────────────────
   Same contract as the placeholder build: every function is async and resolves
   to {ok:true, data} or {ok:false, error}. Errors are values, never throws, and
   the error string is written for a person to read.

   This file is the only file that changed. Everything Telegram-shaped lives
   behind the bridge: command filtering, media proxying, FLOOD_WAIT queueing and
   int64 handling are all server-side. See BACKEND.md.
   ───────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var ok = function (data) { return { ok: true, data: data }; };
  var err = function (error) { return { ok: false, error: error }; };

  var NETWORK_ERROR = 'Could not reach the Telegram bridge. Check your connection and retry.';
  var SESSION_ERROR = 'Your Telethongram session expired — reloading the sign-in page.';

  /* ── boot payload ────────────────────────────────────────────────────────
     The UI calls API.me() and API.userById() synchronously during its first
     render, so the identity payload has to be in hand before this file
     finishes evaluating. One blocking request at load, never again. */
  var boot = { me: null, contacts: [], settings: {}, csrf: '', handlers: [] };
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/bootstrap', false);
    xhr.send(null);
    if (xhr.status === 200) {
      var parsed = JSON.parse(xhr.responseText);
      if (parsed && parsed.ok) boot = parsed.data;
    } else if (xhr.status === 401) {
      window.location.href = '/login';
    }
  } catch (e) {
    /* Leave boot empty; every call will surface the network error instead. */
  }

  var CSRF = boot.csrf || '';
  var me = boot.me || {
    id: 'u_me', name: 'You', initials: 'YOU', username: null, phone: null,
    bio: '', avatarUrl: null, online: true
  };

  /* Synchronous user cache. Seeded from the boot payload, then kept warm by
     every chat, message and profile that passes through. */
  var userCache = {};
  var rememberUser = function (u) {
    if (u && u.id) userCache[u.id] = u;
  };
  rememberUser(me);
  (boot.contacts || []).forEach(rememberUser);

  var learnFromMessage = function (m) {
    if (!m || !m.senderId) return;
    if (!userCache[m.senderId]) {
      userCache[m.senderId] = {
        id: m.senderId, name: m.senderName || 'Unknown',
        initials: initialsOf(m.senderName), avatarUrl: null, online: false
      };
    }
  };
  var initialsOf = function (name) {
    var parts = String(name || '').split(/\s+/).filter(Boolean);
    if (!parts.length) return '??';
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  };

  /* ── chat id aliasing ────────────────────────────────────────────────────
     The shell opens a hard-coded chat on desktop first load. Real accounts do
     not have that id, so it resolves to whichever chat sorts first. */
  var BOOT_ALIAS = 'c_city';
  var aliasTarget = null;
  var knownChats = {};
  var resolveChat = function (chatId) {
    if (chatId === BOOT_ALIAS && !knownChats[BOOT_ALIAS] && aliasTarget) return aliasTarget;
    return chatId;
  };
  var noteChats = function (chats) {
    knownChats = {};
    (chats || []).forEach(function (c) { knownChats[c.id] = true; });
    var sorted = (chats || []).slice().sort(function (a, b) {
      if (!!b.pinned !== !!a.pinned) return b.pinned ? 1 : -1;
      var ad = (a.lastMessage && a.lastMessage.date) || a.addedAt || 0;
      var bd = (b.lastMessage && b.lastMessage.date) || b.addedAt || 0;
      return bd - ad;
    });
    var live = sorted.filter(function (c) { return !c.archived; });
    aliasTarget = (live[0] || sorted[0] || {}).id || null;
    (chats || []).forEach(function (c) {
      if (c.lastMessage) learnFromMessage(c.lastMessage);
    });
  };

  /* ── transport ───────────────────────────────────────────────────────── */
  var failNext = {};
  var redirecting = false;

  var call = function (method, args) {
    if (failNext[method]) { failNext[method] = false; return Promise.resolve(err('Simulated failure (API.__fail).')); }
    return fetch('/api/rpc', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ method: method, args: args || [] })
    }).then(function (res) {
      if (res.status === 401) {
        if (!redirecting) { redirecting = true; setTimeout(function () { window.location.href = '/login'; }, 400); }
        return err(SESSION_ERROR);
      }
      return res.json().then(function (body) {
        if (body && typeof body.ok === 'boolean') return body;
        return err(NETWORK_ERROR);
      }, function () { return err(NETWORK_ERROR); });
    }, function () {
      return err(NETWORK_ERROR);
    });
  };

  /* Debounce for the handful of calls the UI fires on every keystroke. */
  var debounced = {};
  var debounce = function (key, ms, fn) {
    if (debounced[key]) clearTimeout(debounced[key]);
    debounced[key] = setTimeout(function () { debounced[key] = null; fn(); }, ms);
  };

  /* Outgoing messages we already painted, so the socket echo is dropped
     instead of producing a second bubble. */
  var ownEchoes = {};
  var noteOwnEcho = function (m) {
    if (!m || !m.id) return;
    ownEchoes[m.id] = Date.now();
    Object.keys(ownEchoes).forEach(function (k) {
      if (Date.now() - ownEchoes[k] > 120000) delete ownEchoes[k];
    });
  };

  var API = {
    HANDLER_NAMES: boot.handlers && boot.handlers.length ? boot.handlers : [
      'onNewMessage', 'onMessageEdited', 'onMessageDeleted', 'onReaction', 'onTyping',
      'onPresence', 'onReadReceipt', 'onChatAdded', 'onChatRemoved', 'onConnectionChange'
    ],
    me: function () { return Object.assign({}, me); },
    userById: function (id) {
      return userCache[id] || { id: id, name: 'Unknown', initials: '??', avatarUrl: null };
    },
    __fail: function (name) { failNext[name] = true; },

    /* ── reads ─────────────────────────────────────────────────────────── */
    getChats: function () {
      return call('getChats', []).then(function (r) {
        if (r.ok) noteChats(r.data);
        return r;
      });
    },
    getChat: function (chatId) { return call('getChat', [resolveChat(chatId)]); },
    getMessages: function (chatId, opts) {
      var o = opts || {};
      return call('getMessages', [resolveChat(chatId), { before: o.before || null, limit: o.limit || 40 }])
        .then(function (r) {
          if (r.ok) (r.data || []).forEach(learnFromMessage);
          return r;
        });
    },
    getProfile: function (userId) {
      return call('getProfile', [userId]).then(function (r) {
        if (r.ok) rememberUser(r.data);
        return r;
      });
    },
    getSharedMedia: function (chatId, type) {
      return call('getSharedMedia', type ? [resolveChat(chatId), type] : [resolveChat(chatId)]);
    },
    searchAll: function (query) {
      if (!String(query || '').trim()) return Promise.resolve(ok({ chats: [], messages: [], contacts: [] }));
      return call('searchAll', [query]).then(function (r) {
        if (r.ok) {
          (r.data.contacts || []).forEach(rememberUser);
          (r.data.messages || []).forEach(learnFromMessage);
        }
        return r;
      });
    },
    searchInChat: function (chatId, query) {
      if (!String(query || '').trim()) return Promise.resolve(ok([]));
      return call('searchInChat', [resolveChat(chatId), query]);
    },
    getContacts: function () {
      return call('getContacts', []).then(function (r) {
        if (r.ok) (r.data || []).forEach(rememberUser);
        return r;
      });
    },
    getSettings: function () { return call('getSettings', []); },
    getBlocked: function () {
      return call('getBlocked', []).then(function (r) {
        if (r.ok) (r.data || []).forEach(rememberUser);
        return r;
      });
    },
    getSessions: function () { return call('getSessions', []); },

    /* ── writes ────────────────────────────────────────────────────────── */
    sendMessage: function (chatId, payload) {
      return call('sendMessage', [resolveChat(chatId), payload || {}]).then(function (r) {
        if (r.ok) { noteOwnEcho(r.data); learnFromMessage(r.data); }
        return r;
      });
    },
    editMessage: function (chatId, messageId, text) {
      return call('editMessage', [resolveChat(chatId), messageId, text]);
    },
    deleteMessage: function (chatId, messageId, forEveryone) {
      return call('deleteMessage', [resolveChat(chatId), messageId, !!forEveryone]);
    },
    reactToMessage: function (chatId, messageId, emoji) {
      return call('reactToMessage', [resolveChat(chatId), messageId, emoji]);
    },
    forwardMessages: function (fromChatId, messageIds, toChatId) {
      return call('forwardMessages', [resolveChat(fromChatId), messageIds || [], resolveChat(toChatId)]);
    },
    votePoll: function (chatId, messageId, index) {
      return call('votePoll', [resolveChat(chatId), messageId, index]);
    },
    pinMessage: function (chatId, messageId) {
      return call('pinMessage', [resolveChat(chatId), messageId]);
    },
    markRead: function (chatId) { return call('markRead', [resolveChat(chatId)]); },

    /* Fire-and-forget. Throttled by the UI, coalesced again here. */
    setTyping: function (chatId) {
      debounce('typing:' + chatId, 300, function () { call('setTyping', [resolveChat(chatId)]); });
      return Promise.resolve(ok({ chatId: chatId }));
    },
    saveDraft: function (chatId, text) {
      debounce('draft:' + chatId, 500, function () { call('saveDraft', [resolveChat(chatId), text]); });
      return Promise.resolve(ok({ chatId: chatId }));
    },

    /* The shell hands over a descriptor, not a File, so open a real picker and
       upload actual bytes. Progress comes from XHR upload events. */
    uploadFile: function (file, onProgress) {
      var hasBytes = file && (typeof Blob !== 'undefined') && (file instanceof Blob);
      var pick = hasBytes ? Promise.resolve(file) : pickFile(file && file.name);
      return pick.then(function (chosen) {
        if (!chosen) return err('No file was chosen, so nothing was sent.');
        return uploadWithProgress(chosen, onProgress);
      });
    },

    pinChat: function (chatId) { return call('pinChat', [resolveChat(chatId)]); },
    muteChat: function (chatId) { return call('muteChat', [resolveChat(chatId)]); },
    archiveChat: function (chatId) { return call('archiveChat', [resolveChat(chatId)]); },
    clearHistory: function (chatId) { return call('clearHistory', [resolveChat(chatId)]); },

    /* Removes the chat from Telethongram only. The Telegram chat is untouched. */
    deleteChat: function (chatId) { return call('deleteChat', [resolveChat(chatId)]); },

    createChat: function (kind, title, memberIds) {
      return call('createChat', [kind, title, memberIds || []]);
    },
    addMembers: function (chatId, memberIds) {
      return call('addMembers', [resolveChat(chatId), memberIds || []]);
    },
    setPermission: function (chatId, key, value) {
      return call('setPermission', [resolveChat(chatId), key, !!value]);
    },
    setSignMessages: function (chatId, value) {
      return call('setSignMessages', [resolveChat(chatId), !!value]);
    },
    blockUser: function (userId) { return call('blockUser', [userId]); },
    unblockUser: function (userId) { return call('unblockUser', [userId]); },
    updateSettings: function (payload) { return call('updateSettings', [payload || {}]); },

    /* The settings panel calls this on every keystroke; coalesce so Telegram
       does not see one profile edit per character. */
    updateProfile: function (payload) {
      Object.assign(me, payload || {});
      me.initials = initialsOf(me.name);
      rememberUser(me);
      var snapshot = Object.assign({}, payload || {});
      debounce('profile', 1200, function () {
        call('updateProfile', [snapshot]).then(function (r) {
          if (r.ok) { me = r.data; rememberUser(me); }
        });
      });
      return Promise.resolve(ok(Object.assign({}, me)));
    },
    terminateSession: function (id) { return call('terminateSession', [id]); },

    /* ── sign-in ───────────────────────────────────────────────────────────
       Telegram sign-in happens once, on the server, over SSH — never through
       the browser, so no code-request endpoint is exposed to the internet.
       "Log out" in the settings panel lands here, so it ends the web session. */
    requestCode: function () {
      return fetch('/api/logout', {
        method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': CSRF }
      }).then(function () {
        window.location.href = '/login';
        return ok({ sent: false });
      }, function () {
        return err('Could not reach the bridge to sign out.');
      });
    },
    verifyCode: function () {
      return Promise.resolve(err('Sign-in happens on the Telethongram sign-in page, not here.'));
    },
    setName: function (first, last) {
      return API.updateProfile({ name: String(first || '') + ' ' + String(last || '') });
    },

    /* ── subscription ──────────────────────────────────────────────────── */
    subscribeToUpdates: function (handlers) {
      var h = handlers || {};
      var closed = false;
      var socket = null;
      var attempt = 0;
      var fire = function (name, payload) {
        if (typeof h[name] === 'function') {
          try { h[name](payload); } catch (e) { console.error(e); }
        }
      };

      var connect = function () {
        if (closed) return;
        fire('onConnectionChange', { status: attempt ? 'reconnecting' : 'connecting' });
        var proto = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
        try {
          socket = new WebSocket(proto + window.location.host + '/ws');
        } catch (e) {
          scheduleRetry();
          return;
        }
        socket.onmessage = function (ev) {
          var frame;
          try { frame = JSON.parse(ev.data); } catch (e) { return; }
          if (!frame || !frame.event) return;
          var p = frame.payload || {};

          if (frame.event === 'onNewMessage') {
            if (p.message && ownEchoes[p.message.id]) return;   // already painted
            learnFromMessage(p.message);
          }
          if (frame.event === 'onChatAdded' && p.chat) knownChats[p.chat.id] = true;
          if (frame.event === 'onChatRemoved' && p.chatId) delete knownChats[p.chatId];
          if (frame.event === 'onPresence' && p.userId && userCache[p.userId]) {
            userCache[p.userId].online = p.online;
            userCache[p.userId].lastSeen = p.lastSeen;
          }
          if (frame.event === 'settings') return;               // no client handler
          fire(frame.event, p);
        };
        socket.onopen = function () { attempt = 0; };
        socket.onclose = function () { if (!closed) scheduleRetry(); };
        socket.onerror = function () { /* onclose follows */ };
      };

      var scheduleRetry = function () {
        attempt += 1;
        if (attempt > 8) { fire('onConnectionChange', { status: 'offline' }); return; }
        fire('onConnectionChange', { status: 'reconnecting' });
        setTimeout(connect, Math.min(15000, 500 * Math.pow(2, attempt)) + Math.random() * 400);
      };

      connect();
      return function () {
        closed = true;
        if (socket) { try { socket.close(); } catch (e) {} }
      };
    }
  };

  /* ── upload helpers ────────────────────────────────────────────────────── */
  function pickFile(hintName) {
    return new Promise(function (resolve) {
      var input = document.createElement('input');
      input.type = 'file';
      var ext = String(hintName || '').split('.').pop().toLowerCase();
      if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'mp4', 'mov'].indexOf(ext) > -1) {
        input.accept = 'image/*,video/*';
      }
      input.style.position = 'fixed';
      input.style.left = '-9999px';
      document.body.appendChild(input);
      var done = function (value) {
        if (input.parentNode) input.parentNode.removeChild(input);
        resolve(value);
      };
      input.onchange = function () { done(input.files && input.files[0]); };
      window.addEventListener('focus', function once() {
        window.removeEventListener('focus', once);
        setTimeout(function () { if (!input.files || !input.files.length) done(null); }, 500);
      });
      input.click();
    });
  }

  function uploadWithProgress(file, onProgress) {
    return new Promise(function (resolve) {
      var form = new FormData();
      form.append('file', file, file.name || 'upload.bin');
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload', true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-CSRF-Token', CSRF);
      xhr.upload.onprogress = function (e) {
        if (typeof onProgress === 'function' && e.lengthComputable) {
          onProgress({
            loaded: e.loaded, total: e.total,
            percent: Math.round((e.loaded / e.total) * 100)
          });
        }
      };
      xhr.onload = function () {
        if (typeof onProgress === 'function') {
          onProgress({ loaded: file.size, total: file.size, percent: 100 });
        }
        if (xhr.status === 401) { window.location.href = '/login'; return resolve(err(SESSION_ERROR)); }
        try {
          var body = JSON.parse(xhr.responseText);
          resolve(body && typeof body.ok === 'boolean' ? body : err('The upload failed.'));
        } catch (e) {
          resolve(err('The upload failed.'));
        }
      };
      xhr.onerror = function () { resolve(err(NETWORK_ERROR)); };
      xhr.send(form);
    });
  }

  window.API = API;
})();
