/* Auth-only ambient source network — login / register pages. */
(function () {
  var host = document.getElementById('auth-network');
  if (!host) return;

  var svgNS = 'http://www.w3.org/2000/svg';
  var resizeTimer = null;
  var liveTimer = null;
  var fadeTimer = null;
  var loopTimer = null;
  var pendingRestart = false;
  var lastW = 0;
  var lastH = 0;

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  function dist(a, b) {
    var dx = a.x - b.x;
    var dy = a.y - b.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  /** Even lattice covering nearly the full viewport (including top/bottom). */
  function gridPlan(w, h) {
    var padX = Math.max(18, w * 0.025);
    var padY = Math.max(16, h * 0.028);
    var usableW = Math.max(120, w - padX * 2);
    var usableH = Math.max(120, h - padY * 2);
    var area = w * h;
    var target;
    if (area < 480000) target = 18;
    else if (area < 900000) target = 28;
    else if (area < 1400000) target = 36;
    else target = 44;

    var aspect = usableW / Math.max(1, usableH);
    var cols = Math.max(3, Math.round(Math.sqrt(target * aspect)));
    var rows = Math.max(3, Math.round(target / cols));

    if (cols * rows < target - 2) {
      if (usableW / cols >= usableH / rows) cols += 1;
      else rows += 1;
    }
    while (cols * rows > target + 5) {
      if (cols >= rows && cols > 3) cols -= 1;
      else if (rows > 3) rows -= 1;
      else break;
    }

    return {
      padX: padX,
      padY: padY,
      cols: cols,
      rows: rows,
      cellW: usableW / cols,
      cellH: usableH / rows
    };
  }

  function placeNodes(w, h) {
    var plan = gridPlan(w, h);
    var nodes = [];
    var jitterX = plan.cellW * 0.28;
    var jitterY = plan.cellH * 0.28;
    var kindFlip = Math.random() < 0.5;

    for (var r = 0; r < plan.rows; r++) {
      for (var c = 0; c < plan.cols; c++) {
        var baseX = plan.padX + (c + 0.5) * plan.cellW;
        var baseY = plan.padY + (r + 0.5) * plan.cellH;
        var web = ((r + c) % 2 === 0) === kindFlip;
        if (Math.random() < 0.12) web = !web;

        nodes.push({
          x: baseX + rand(-jitterX, jitterX),
          y: baseY + rand(-jitterY, jitterY),
          kind: web ? 'web' : 'social',
          r: rand(2.15, 3.35),
          row: r,
          col: c
        });
      }
    }

    var minGap = Math.min(plan.cellW, plan.cellH) * 0.58;
    for (var pass = 0; pass < 3; pass++) {
      for (var i = 0; i < nodes.length; i++) {
        for (var j = i + 1; j < nodes.length; j++) {
          var a = nodes[i];
          var b = nodes[j];
          var d = dist(a, b);
          if (d >= minGap || d < 0.001) continue;
          var push = (minGap - d) * 0.5;
          var ux = (a.x - b.x) / d;
          var uy = (a.y - b.y) / d;
          a.x += ux * push;
          a.y += uy * push;
          b.x -= ux * push;
          b.y -= uy * push;
        }
      }
    }

    for (var k = 0; k < nodes.length; k++) {
      nodes[k].x = Math.min(w - plan.padX, Math.max(plan.padX, nodes[k].x));
      nodes[k].y = Math.min(h - plan.padY, Math.max(plan.padY, nodes[k].y));
    }

    return { nodes: nodes, cellSpan: Math.hypot(plan.cellW, plan.cellH) };
  }

  function nearestEdges(nodes, k, maxDist) {
    var edges = [];
    var seen = {};
    for (var i = 0; i < nodes.length; i++) {
      var scored = [];
      for (var j = 0; j < nodes.length; j++) {
        if (i === j) continue;
        var d = dist(nodes[i], nodes[j]);
        if (d > maxDist) continue;
        scored.push({ j: j, d: d });
      }
      scored.sort(function (a, b) { return a.d - b.d; });
      var take = Math.min(k, scored.length);
      for (var t = 0; t < take; t++) {
        var a = Math.min(i, scored[t].j);
        var b = Math.max(i, scored[t].j);
        var key = a + '-' + b;
        if (seen[key]) continue;
        seen[key] = true;
        edges.push({ a: a, b: b });
      }
    }
    return edges;
  }

  function el(name, attrs) {
    var node = document.createElementNS(svgNS, name);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        node.setAttribute(key, attrs[key]);
      });
    }
    return node;
  }

  function build(force) {
    var w = window.innerWidth || 1200;
    var h = window.innerHeight || 800;
    if (!force && Math.abs(w - lastW) < 48 && Math.abs(h - lastH) < 48 && host.firstChild) {
      return;
    }
    lastW = w;
    lastH = h;
    clearTimeout(liveTimer);
    clearTimeout(fadeTimer);
    clearTimeout(loopTimer);
    pendingRestart = false;

    var placed = placeNodes(w, h);
    var nodes = placed.nodes;
    var maxLink = placed.cellSpan * 1.85;
    var edges = nearestEdges(nodes, w < 700 ? 2 : 3, maxLink);
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // Reveal top → bottom so the full height fills in smoothly
    var order = nodes
      .map(function (n, idx) { return { idx: idx, score: n.y + n.x * 0.15 }; })
      .sort(function (a, b) { return a.score - b.score; });
    var appearAt = new Array(nodes.length);
    var step = reduce ? 0 : 0.085;
    order.forEach(function (item, rank) {
      appearAt[item.idx] = rank * step;
    });

    var svg = el('svg', {
      class: 'auth-network-svg',
      viewBox: '0 0 ' + w + ' ' + h,
      width: '100%',
      height: '100%',
      preserveAspectRatio: 'xMidYMid slice',
      role: 'presentation'
    });

    var linesG = el('g', { class: 'auth-net-lines' });
    edges.forEach(function (e, idx) {
      var n1 = nodes[e.a];
      var n2 = nodes[e.b];
      var line = el('line', {
        x1: n1.x.toFixed(1),
        y1: n1.y.toFixed(1),
        x2: n2.x.toFixed(1),
        y2: n2.y.toFixed(1),
        class: 'auth-net-line'
      });
      var lineDelay = Math.max(appearAt[e.a], appearAt[e.b]) + 0.12;
      line.style.setProperty('--appear-delay', lineDelay.toFixed(2) + 's');
      if (!reduce) {
        line.style.setProperty('--pulse-delay', (-rand(0, 10)).toFixed(2) + 's');
        line.style.setProperty('--pulse-dur', (9 + (idx % 5) * 1.4).toFixed(1) + 's');
      }
      linesG.appendChild(line);
    });
    svg.appendChild(linesG);

    var nodesG = el('g', { class: 'auth-net-nodes' });
    var flashBudget = Math.max(2, Math.min(5, Math.floor(nodes.length * 0.16)));
    var flashIdx = {};
    var flashStep = Math.max(1, Math.floor(nodes.length / flashBudget));
    var flashStart = Math.floor(Math.random() * flashStep);
    for (var f = 0; f < flashBudget; f++) {
      flashIdx[(flashStart + f * flashStep) % nodes.length] = true;
    }

    nodes.forEach(function (n, idx) {
      var g = el('g', {
        class: 'auth-net-node auth-net-node--' + n.kind + (flashIdx[idx] ? ' auth-net-node--flash' : ''),
        transform: 'translate(' + n.x.toFixed(1) + ' ' + n.y.toFixed(1) + ')'
      });
      var inner = el('g', { class: 'auth-net-node-inner' });
      inner.style.setProperty('--appear-delay', appearAt[idx].toFixed(2) + 's');

      var core = el('circle', {
        r: n.r.toFixed(2),
        class: 'auth-net-core'
      });
      var halo = el('circle', {
        r: (n.r * 2.4).toFixed(2),
        class: 'auth-net-halo'
      });
      if (!reduce) {
        var pulseDelay = (-rand(0, 8)).toFixed(2) + 's';
        var pulseDur = (5.5 + (idx % 6) * 0.7).toFixed(1) + 's';
        if (flashIdx[idx]) {
          pulseDelay = (rand(1.5, 14)).toFixed(2) + 's';
          pulseDur = (7 + (idx % 4) * 2.2).toFixed(1) + 's';
        }
        core.style.setProperty('--pulse-delay', pulseDelay);
        core.style.setProperty('--pulse-dur', pulseDur);
        halo.style.setProperty('--pulse-delay', pulseDelay);
        halo.style.setProperty('--pulse-dur', pulseDur);
      }
      inner.appendChild(halo);
      inner.appendChild(core);
      g.appendChild(inner);
      nodesG.appendChild(g);
    });
    svg.appendChild(nodesG);

    // Sweep lives outside the rebuilt SVG so it never resets with the dots
    var sweep = host.querySelector('.auth-net-sweep');
    if (!sweep) {
      sweep = document.createElement('div');
      sweep.className = 'auth-net-sweep';
      sweep.setAttribute('aria-hidden', 'true');
      host.appendChild(sweep);
    }

    var oldSvg = host.querySelector('.auth-network-svg');
    if (oldSvg) oldSvg.replaceWith(svg);
    else host.insertBefore(svg, sweep);

    host.classList.toggle('is-reduced', reduce);
    host.classList.remove('is-live', 'is-fading');
    host.classList.add('is-forming');

    if (reduce) {
      host.classList.remove('is-forming');
      host.classList.add('is-live');
    } else {
      var formMs = (nodes.length * step + 1.05) * 1000;
      // Form → brief life → fade → form again (same visual language, fresh layout)
      liveTimer = setTimeout(function () {
        if (document.hidden) { pendingRestart = true; return; }
        host.classList.remove('is-forming');
        host.classList.add('is-live');
        fadeTimer = setTimeout(function () {
          if (document.hidden) { pendingRestart = true; return; }
          host.classList.remove('is-live');
          host.classList.add('is-fading');
          loopTimer = setTimeout(function () {
            if (document.hidden) { pendingRestart = true; return; }
            build(true);
          }, 950);
        }, 2600);
      }, formMs);
    }
    syncVisibility();
  }

  function syncVisibility() {
    var hidden = document.hidden;
    host.classList.toggle('is-paused', hidden);
    if (!hidden && pendingRestart && !host.classList.contains('is-reduced')) {
      pendingRestart = false;
      build(true);
    }
  }

  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { build(true); }, 180);
  }

  build(true);
  window.addEventListener('resize', onResize, { passive: true });
  document.addEventListener('visibilitychange', syncVisibility);
})();
