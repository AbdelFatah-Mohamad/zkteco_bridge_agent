(function () {
  function fmtAgo(ts) {
    if (!ts) { return '–'; }
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    return s < 90 ? s + 's' : Math.round(s / 60) + 'm';
  }
  function refresh() {
    fetch('api/status').then(function (r) { return r.json(); }).then(function (d) {
      var set = function (id, text) {
        var el = document.getElementById(id);
        if (el) { el.textContent = text; }
      };
      var stateEl = document.getElementById('state');
      var labels = stateEl ? JSON.parse(stateEl.dataset.labels || '{}') : {};
      set('state', labels[d.state] || d.state || '–');
      set('msg', d.message || '');
      set('last', fmtAgo(d.last_poll_at));
      set('next', d.next_poll_at ? Math.max(0, Math.round(d.next_poll_at - Date.now() / 1000)) + 's' : '–');
      set('spool', (d.spool && d.spool.pending) || 0);
      var list = document.getElementById('running');
      if (list) {
        var jobs = (d.snapshot && d.snapshot.running) || [];
        list.innerHTML = '';
        if (!jobs.length) {
          var li = document.createElement('li');
          li.className = 'hint';
          li.textContent = '—';
          list.appendChild(li);
        }
        jobs.forEach(function (job) {
          var li = document.createElement('li');
          li.textContent = job.operation + ' #' + job.job_id;
          list.appendChild(li);
        });
      }
    }).catch(function () { /* the page stays as it was */ });
  }
  if (document.getElementById('state')) {
    refresh();
    setInterval(refresh, 4000);
  }
  document.querySelectorAll('button.test').forEach(function (button) {
    button.addEventListener('click', function () {
      button.disabled = true;
      fetch('api/test-device', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': button.dataset.csrf },
        body: JSON.stringify({ device_key: button.dataset.key })
      }).then(function () { setTimeout(function () { location.reload(); }, 3000); });
    });
  });
}());
