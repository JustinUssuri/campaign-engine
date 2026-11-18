/* 地图集的“画”：把一张本地图（atlas.json 的 layers.X）画成一张 SVG，交给 Leaflet 当底图。
   只画地貌、城墙、街道、街区；地点圆点、标签、路线还是 atlas.html 用 Leaflet 画（大小不随缩放变）。
   随机数按地点 ID 定种子：同样的数据每次画出来一样，加了新地点也只动它附近。 */
(function () {
  'use strict';

  // ---------- 小工具 ----------
  function rng(seed) {                       // mulberry32，种子由字符串哈希来
    let h = 2166136261;
    for (const c of String(seed)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    return () => {
      h += 0x6D2B79F5;
      let t = h;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const f1 = n => Math.round(n * 10) / 10;
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

  // 折线加点、往两侧轻轻抖，像手画的
  function wiggle(pts, step, amp, rand) {
    const out = [pts[0]];
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i], L = dist(a, b), n = Math.max(1, Math.round(L / step));
      const nx = -(b[1] - a[1]) / (L || 1), ny = (b[0] - a[0]) / (L || 1);
      for (let k = 1; k < n; k++) {
        const t = k / n, o = (rand() - 0.5) * amp * Math.sin(Math.PI * t);
        out.push([a[0] + (b[0] - a[0]) * t + nx * o, a[1] + (b[1] - a[1]) * t + ny * o]);
      }
      out.push(b);
    }
    return out;
  }
  // Catmull-Rom → 三次贝塞尔的 SVG path
  function curve(pts, closed) {
    const n = pts.length;
    if (n < 2) return '';
    const P = i => closed ? pts[(i + n) % n] : pts[Math.max(0, Math.min(n - 1, i))];
    let d = `M${f1(pts[0][0])} ${f1(pts[0][1])}`;
    const last = closed ? n : n - 1;
    for (let i = 0; i < last; i++) {
      const p0 = P(i - 1), p1 = P(i), p2 = P(i + 1), p3 = P(i + 2);
      d += `C${f1(p1[0] + (p2[0] - p0[0]) / 6)} ${f1(p1[1] + (p2[1] - p0[1]) / 6)} ` +
           `${f1(p2[0] - (p3[0] - p1[0]) / 6)} ${f1(p2[1] - (p3[1] - p1[1]) / 6)} ${f1(p2[0])} ${f1(p2[1])}`;
    }
    return closed ? d + 'Z' : d;
  }
  // 给 Leaflet 用：曲线上取点（[x, y] 地图单位）
  function samplePath(pts, per = 8) {
    if (pts.length < 3) return pts;
    const out = [], n = pts.length, P = i => pts[Math.max(0, Math.min(n - 1, i))];
    for (let i = 0; i < n - 1; i++) {
      const p0 = P(i - 1), p1 = P(i), p2 = P(i + 1), p3 = P(i + 2);
      for (let k = 0; k < per; k++) {
        const t = k / per, t2 = t * t, t3 = t2 * t;
        const f = (a, b, c, e) => 0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - e) * t2 + (-a + 3 * b - 3 * c + e) * t3);
        out.push([f(p0[0], p1[0], p2[0], p3[0]), f(p0[1], p1[1], p2[1], p3[1])]);
      }
    }
    out.push(pts[n - 1]);
    return out;
  }
  function inPoly(p, poly) {
    let c = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const a = poly[i], b = poly[j];
      if ((a[1] > p[1]) !== (b[1] > p[1]) && p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]) c = !c;
    }
    return c;
  }
  function nearestOnPolyline(p, line) {
    let best = null, bd = Infinity;
    for (let i = 1; i < line.length; i++) {
      const a = line[i - 1], b = line[i], dx = b[0] - a[0], dy = b[1] - a[1];
      const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
      const q = [a[0] + dx * t, a[1] + dy * t], d = dist(p, q);
      if (d < bd) { bd = d; best = q; }
    }
    return [best, bd];
  }
  // 墙：环上按 step 重新取点（去掉挤在一起的点）
  function resampleRing(ring, step) {
    const pts = [];
    for (let i = 0; i < ring.length; i++) {
      const a = ring[i], b = ring[(i + 1) % ring.length], n = Math.max(1, Math.round(dist(a, b) / step));
      for (let k = 0; k < n; k++) pts.push([a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n]);
    }
    return pts;
  }

  const area = poly => Math.abs(poly.reduce((s, p, i) => { const q = poly[(i + 1) % poly.length]; return s + p[0] * q[1] - q[0] * p[1]; }, 0)) / 2;
  // 凸多边形按直线（过 o、法向 n）切成两半
  function clipHalf(poly, o, n, keep) {
    const side = p => ((p[0] - o[0]) * n[0] + (p[1] - o[1]) * n[1]) * keep;
    const out = [];
    for (let i = 0; i < poly.length; i++) {
      const a = poly[i], b = poly[(i + 1) % poly.length], sa = side(a), sb = side(b);
      if (sa >= 0) out.push(a);
      if ((sa >= 0) !== (sb >= 0)) { const t = sa / (sa - sb); out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]); }
    }
    return out;
  }
  // 街区切成一栋栋房子：每次垂直于最长边、在中间附近切一刀
  function lots(poly, minA, rand, depth = 0, out = []) {
    if (poly.length < 3) return out;
    const A = area(poly);
    if (A < minA || depth > 5) { out.push(poly); return out; }
    let k = 0, L = 0;
    poly.forEach((p, i) => { const d = dist(p, poly[(i + 1) % poly.length]); if (d > L) { L = d; k = i; } });
    const a = poly[k], b = poly[(k + 1) % poly.length], t = 0.38 + rand() * 0.24;
    const o = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], n = [(b[0] - a[0]) / L, (b[1] - a[1]) / L];
    lots(clipHalf(poly, o, n, 1), minA, rand, depth + 1, out);
    lots(clipHalf(poly, o, n, -1), minA, rand, depth + 1, out);
    return out;
  }
  // 城墙：从城中心看出去，把凸包的半径曲线抹圆（城门处拉回到城门上），再加一点起伏
  function roundWall(hull, gates, rand) {
    const cx = hull.reduce((s, p) => s + p[0], 0) / hull.length, cy = hull.reduce((s, p) => s + p[1], 0) / hull.length;
    const N = 96, R = [];
    for (let i = 0; i < N; i++) {           // 每个方向上凸包有多远
      const th = i / N * Math.PI * 2, dx = Math.cos(th), dy = Math.sin(th);
      let r = 0;
      for (let j = 0; j < hull.length; j++) {
        const a = hull[j], b = hull[(j + 1) % hull.length], ex = b[0] - a[0], ey = b[1] - a[1];
        const den = dx * ey - dy * ex;
        if (Math.abs(den) < 1e-12) continue;
        const t = ((a[0] - cx) * ey - (a[1] - cy) * ex) / den, s = ((a[0] - cx) * dy - (a[1] - cy) * dx) / den;
        if (t > 0 && s >= 0 && s <= 1) r = Math.max(r, t);
      }
      R.push(r);
    }
    const W = 9, sm = R.map((_, i) => { let s = 0; for (let k = -W; k <= W; k++) s += R[(i + k + N) % N]; return s / (2 * W + 1); });
    let out = sm.map((r, i) => Math.max(r, R[i] * 0.97) * (1 + (rand() - 0.5) * 0.03));
    for (const g of gates) {                 // 城门那一带往城门拉
      const th = Math.atan2(g[1] - cy, g[0] - cx), gr = Math.hypot(g[0] - cx, g[1] - cy);
      out = out.map((r, i) => {
        let d = Math.abs(i / N * Math.PI * 2 - ((th + Math.PI * 2) % (Math.PI * 2)));
        d = Math.min(d, Math.PI * 2 - d);
        const w = Math.exp(-((d / 0.16) ** 2));
        return r * (1 - w) + gr * w;
      });
    }
    return out.map((r, i) => [cx + Math.cos(i / N * Math.PI * 2) * r, cy + Math.sin(i / N * Math.PI * 2) * r]);
  }

  const C = {
    paper: '#efe3c6', ground: '#e8dab6', inner: '#e4d3ab', ink: '#3b2a1a', soft: '#7a6146',
    roof: '#c9ab7c', roofLine: '#9c7d55', casing: '#7d6446', street: '#f2e8cf', wall: '#5a4330',
    tower: '#8a6a48', river: '#4f7891', coast: '#7f97a0', flats: '#d9d0b4', field: '#d8c99a',
  };

  // ---------- defs：花纹 ----------
  function defs(S) {
    const u = v => f1(v * S);                // 英里 → SVG 单位
    let d = '<defs>';
    // 草地：一簇簇短线
    d += `<pattern id="grass" patternUnits="userSpaceOnUse" width="${u(0.35)}" height="${u(0.35)}">`;
    const r = rng('grass');
    for (let i = 0; i < 7; i++) {
      const x = r() * u(0.35), y = r() * u(0.35), h = u(0.012);
      d += `<path d="M${f1(x)} ${f1(y)}l${f1(-h * .4)} ${f1(-h)}M${f1(x)} ${f1(y)}l0 ${f1(-h * 1.2)}M${f1(x)} ${f1(y)}l${f1(h * .4)} ${f1(-h)}" stroke="#a69468" stroke-width="${u(0.002)}" fill="none"/>`;
    }
    d += '</pattern>';
    // 滩涂：细点
    d += `<pattern id="stipple" patternUnits="userSpaceOnUse" width="${u(0.12)}" height="${u(0.12)}">`;
    const r2 = rng('stipple');
    for (let i = 0; i < 9; i++) d += `<circle cx="${f1(r2() * u(0.12))}" cy="${f1(r2() * u(0.12))}" r="${u(0.0035)}" fill="#a39876"/>`;
    d += '</pattern>';
    // 屋顶：分地块的细线（四个朝向）
    for (const [k, rot] of [[0, 0], [1, 32], [2, -27], [3, 71]]) {
      const w = u(0.022);
      d += `<pattern id="roof${k}" patternUnits="userSpaceOnUse" width="${w}" height="${w}" patternTransform="rotate(${rot})">` +
        `<rect width="${w}" height="${w}" fill="${C.roof}"/>` +
        `<path d="M0 ${f1(w / 2)}H${w}M${f1(w * .35)} 0V${f1(w / 2)}M${f1(w * .75)} ${f1(w / 2)}V${w}" stroke="${C.roofLine}" stroke-width="${u(0.0018)}" fill="none"/></pattern>`;
    }
    // 园子
    d += `<pattern id="garden" patternUnits="userSpaceOnUse" width="${u(0.02)}" height="${u(0.02)}">` +
      `<rect width="${u(0.02)}" height="${u(0.02)}" fill="#cfc596"/><circle cx="${u(0.01)}" cy="${u(0.01)}" r="${u(0.004)}" fill="#9e9a63"/></pattern>`;
    // 山：一排排小山尖
    d += `<pattern id="peaks" patternUnits="userSpaceOnUse" width="${u(9)}" height="${u(6)}">` +
      `<path d="M${u(.5)} ${u(5.5)}L${u(2.6)} ${u(1.6)}L${u(4.8)} ${u(5.5)}M${u(4.2)} ${u(5.5)}L${u(6.4)} ${u(2.4)}L${u(8.6)} ${u(5.5)}" ` +
      `fill="none" stroke="${C.soft}" stroke-width="${u(0.35)}" stroke-linejoin="round"/>` +
      `<path d="M${u(2.6)} ${u(1.6)}L${u(3.4)} ${u(5.5)}M${u(6.4)} ${u(2.4)}L${u(7.1)} ${u(5.5)}" stroke="${C.soft}" stroke-width="${u(0.18)}" opacity=".6"/></pattern>`;
    return d + '</defs>';
  }

  // ---------- 一张本地图 ----------
  // 返回 { svg, bounds, smooth }：bounds 是 Leaflet 的 [[y0,x0],[y1,x1]]；smooth(点列) 给路线用
  function draw(layer, opts = {}) {
    const S = opts.scale || 1000;            // 每英里多少 SVG 单位
    const T = ([x, y]) => [x * S, -y * S];   // 地图坐标（y 朝北）→ SVG（y 朝下）
    const nodes = Object.fromEntries(layer.nodes.map(n => [n.id, n]));
    const all = layer.nodes.map(n => [n.x, n.y]).concat(...layer.areas.map(a => a.pts), layer.wall);
    const pad = opts.pad ?? 0.6;
    let x0 = Math.min(...all.map(p => p[0])), x1 = Math.max(...all.map(p => p[0]));
    let y0 = Math.min(...all.map(p => p[1])), y1 = Math.max(...all.map(p => p[1]));
    const span = Math.max(x1 - x0, y1 - y0), px = span * pad + 0.3, py = span * pad + 0.3;
    x0 -= px; x1 += px; y0 -= py; y1 += py;
    const vb = [x0 * S, -y1 * S, (x1 - x0) * S, (y1 - y0) * S].map(f1);
    const u = v => f1(v * S);
    let g = defs(S);
    g += `<rect x="${vb[0]}" y="${vb[1]}" width="${vb[2]}" height="${vb[3]}" fill="${C.ground}"/>`;
    g += `<rect x="${vb[0]}" y="${vb[1]}" width="${vb[2]}" height="${vb[3]}" fill="url(#grass)" opacity=".8"/>`;

    // 地貌片区
    for (const a of layer.areas) {
      const pts = a.pts.map(T), r = rng('area' + a.name);
      const ring = a.style === 'mountains' ? pts : wiggle(pts.concat([pts[0]]), u(0.15) * (S < 100 ? 30 : 1), u(0.04) * (S < 100 ? 30 : 1), r).slice(0, -1);
      if (a.style === 'mountains')
        g += `<path d="${curve(ring, true)}" fill="#dccaa0" opacity=".7"/><path d="${curve(ring, true)}" fill="url(#peaks)"/>`;
      else
        g += `<path d="${curve(ring, true)}" fill="${C.flats}" opacity=".85"/><path d="${curve(ring, true)}" fill="url(#stipple)" stroke="#b0a585" stroke-width="${u(S < 100 ? 0.3 : 0.006)}" stroke-dasharray="${u(S < 100 ? 1 : 0.02)} ${u(S < 100 ? 1 : 0.02)}"/>`;
    }

    // 街道、大路、河、海岸：先算好曲线
    const lines = layer.streets.map(s => {
      const r = rng('street' + s.name);
      const big = s.style === 'road' || s.style === 'river' || s.style === 'wash' || s.style === 'coast';
      const step = big ? (S < 100 ? 4 : 0.3) : 0.06, amp = big ? (S < 100 ? 3 : 0.08) : 0.015;
      const pts = wiggle(s.pts.map(([x, y]) => [x, y]), step, amp, r);
      return { ...s, pts, d: curve(pts.map(T)) };
    });
    const W = { street: [0.03, 0.022], lane: [0.018, 0.012], quay: [0.04, 0.03], road: [0.03, 0.018] };   // 英里；城里的乘 k
    const roadW = S < 100 ? [0.9, 0.5] : W.road;
    // 城外：河、海岸、大路
    for (const l of lines) {
      if (l.style === 'river') {
        g += `<path d="${l.d}" fill="none" stroke="${C.river}" stroke-width="${u(S < 100 ? 1.4 : 0.06)}" stroke-linecap="round" opacity=".85"/>`;
      } else if (l.style === 'wash') {      // 干河床：平时没水，细虚线
        g += `<path d="${l.d}" fill="none" stroke="${C.river}" stroke-width="${u(S < 100 ? 0.8 : 0.035)}" stroke-dasharray="${u(S < 100 ? 2.5 : 0.1)} ${u(S < 100 ? 2 : 0.08)}" stroke-linecap="round" opacity=".6"/>`;
      } else if (l.style === 'coast') {
        g += `<path d="${l.d}" fill="none" stroke="${C.coast}" stroke-width="${u(0.012)}" stroke-dasharray="${u(0.1)} ${u(0.05)}" opacity=".9"/>` +
             `<path d="${l.d}" fill="none" stroke="${C.coast}" stroke-width="${u(0.012)}" transform="translate(${u(0.06)} ${u(0.05)})" opacity=".5"/>`;
      } else if (l.style === 'road') {
        g += `<path d="${l.d}" fill="none" stroke="${C.casing}" stroke-width="${u(roadW[0])}" stroke-linecap="round" opacity=".75"/>` +
             `<path d="${l.d}" fill="none" stroke="${C.street}" stroke-width="${u(roadW[1])}" stroke-linecap="round"/>`;
      }
    }

    // 有城墙的城：城里地面、街区、空场、街道、城墙、地标
    if (layer.wall.length >= 3) {
      // 城小，墙、街、城楼跟着细（德尔齐默约 1.6 英里宽算 1）
      const wxs = layer.wall.map(p => p[0]), wys = layer.wall.map(p => p[1]);
      const k = Math.max(0.4, Math.min(1, Math.min(Math.max(...wxs) - Math.min(...wxs), Math.max(...wys) - Math.min(...wys)) / 1.4));
      const uk = v => u(v * k);
      const r = rng('wall');
      const gatesXY = layer.nodes.filter(n => n.kind === 'gate').map(n => [n.x, n.y]);
      const ring = roundWall(layer.wall, gatesXY, r);
      const wallD = curve(ring.map(T), true);
      g += `<clipPath id="city"><path d="${wallD}"/></clipPath>`;
      g += `<path d="${wallD}" fill="${C.inner}"/>`;

      // 街区：城里撒点 → Voronoi 格子 → 每格一块屋顶（格子边描成地面色，就是小巷）
      const inner = layer.nodes.filter(n => n.in_wall);
      const plazas = layer.nodes.filter(n => n.plaza);
      const xs = ring.map(p => p[0]), ys = ring.map(p => p[1]);
      const bx0 = Math.min(...xs), bx1 = Math.max(...xs), by0 = Math.min(...ys), by1 = Math.max(...ys);
      const rs = rng('blocks'), size = Math.min(bx1 - bx0, by1 - by0), sp = Math.min(0.12, size / 6), seeds = [];
      const edgeNear = Math.min(0.12, size * 0.08);
      for (let y = by0; y <= by1; y += sp * 0.87) {
        for (let x = bx0 + ((Math.round((y - by0) / (sp * 0.87)) % 2) * sp / 2); x <= bx1; x += sp) {
          const p = [x + (rs() - 0.5) * sp * 0.9, y + (rs() - 0.5) * sp * 0.9];
          if (!inPoly(p, ring)) continue;
          if (plazas.some(n => dist(p, [n.x, n.y]) < n.plaza + 0.03)) continue;
          seeds.push(p);
        }
      }
      if (seeds.length > 3 && window.d3 && d3.Delaunay) {
        const vor = d3.Delaunay.from(seeds).voronoi([bx0 - .1, by0 - .1, bx1 + .1, by1 + .1]);
        const shades = ['#cfb185', '#c6a678', '#d6bb92', '#bfa070'];
        let blocks = '', houses = '';
        seeds.forEach((p, i) => {
          const cell = vor.cellPolygon(i);
          if (!cell) return;
          const poly = cell.slice(0, -1);
          // 离城墙近的街区稀一些，有园子、空地
          const edge = Math.min(...ring.map(q => dist(q, p)));
          const h = rs();
          if (h < (edge < edgeNear ? 0.3 : 0.06)) { blocks += `<path d="M${poly.map(T).map(q => q.map(f1).join(' ')).join('L')}Z" fill="url(#garden)" stroke="${C.inner}" stroke-width="${uk(0.012)}"/>`; return; }
          blocks += `<path d="M${poly.map(T).map(q => q.map(f1).join(' ')).join('L')}Z" fill="#b89b70" stroke="${C.inner}" stroke-width="${uk(0.014)}"/>`;
          for (const lot of lots(poly, edge < edgeNear ? 0.0011 : 0.0006, rs)) {
            if (rs() < 0.08) continue;          // 院子
            houses += `<path d="M${lot.map(T).map(q => q.map(f1).join(' ')).join('L')}Z" fill="${shades[Math.floor(rs() * shades.length)]}"/>`;
          }
        });
        g += `<g clip-path="url(#city)">${blocks}<g stroke="#8d714c" stroke-width="${uk(0.0022)}" stroke-linejoin="round">${houses}</g></g>`;
      }
      // 集市空场：不规则的一圈空地，摊棚点几个
      for (const n of plazas) {
        const rp = rng('plaza' + n.id), k = 9, pts = [];
        for (let i = 0; i < k; i++) {
          const a = i / k * Math.PI * 2, rr = n.plaza * (0.85 + rp() * 0.3);
          pts.push(T([n.x + Math.cos(a) * rr, n.y + Math.sin(a) * rr]));
        }
        g += `<path d="${curve(pts, true)}" fill="${C.street}" stroke="${C.casing}" stroke-width="${uk(0.003)}"/>`;
        for (let i = 0; i < 5; i++) {
          const a = rp() * Math.PI * 2, rr = rp() * n.plaza * 0.6, [sx, sy] = T([n.x + Math.cos(a) * rr, n.y + Math.sin(a) * rr]);
          g += `<rect x="${f1(sx)}" y="${f1(sy)}" width="${uk(0.008)}" height="${uk(0.006)}" fill="#b89a6a" transform="rotate(${Math.round(rp() * 90)} ${f1(sx)} ${f1(sy)})"/>`;
        }
      }
      // 小巷：城里没挨着街的地点，接到最近的街上
      const streetLines = lines.filter(l => ['street', 'lane', 'quay'].includes(l.style)).map(l => samplePath(l.pts, 6));
      const lanes = [];
      for (const n of inner) {
        if (n.kind === 'gate' || n.kind === 'junction' || !streetLines.length) continue;
        let best = null, bd = Infinity;
        for (const sl of streetLines) { const [q, d] = nearestOnPolyline([n.x, n.y], sl); if (d < bd) { bd = d; best = q; } }
        if (bd > 0.015 && bd < 0.5) lanes.push(wiggle([[n.x, n.y], best], 0.05, 0.02, rng('lane' + n.id)));
      }
      const all = [...lines.filter(l => ['street', 'lane', 'quay'].includes(l.style)).map(l => [l.style, l.d]),
                   ...lanes.map(p => ['lane', curve(p.map(T))])];
      g += '<g clip-path="url(#city)">';
      for (const [st, d] of all) g += `<path d="${d}" fill="none" stroke="${st === 'quay' ? '#6d6253' : C.casing}" stroke-width="${uk(W[st][0])}" stroke-linecap="round" stroke-linejoin="round"/>`;
      for (const [st, d] of all) g += `<path d="${d}" fill="none" stroke="${st === 'quay' ? '#ddd3bf' : C.street}" stroke-width="${uk(W[st][1])}" stroke-linecap="round" stroke-linejoin="round"/>`;
      g += '</g>';
      // 石码头外沿的系船石桩
      for (const l of lines.filter(l => l.style === 'quay')) {
        const sp2 = samplePath(l.pts, 6);
        for (let i = 2; i < sp2.length - 1; i += 3) {
          const [qx, qy] = T(sp2[i]);
          g += `<circle cx="${f1(qx)}" cy="${f1(qy + uk(0.022))}" r="${uk(0.004)}" fill="${C.ink}"/>`;
        }
      }
      // 城墙、城楼、城门
      // 沿河那一段不砌墙（码头直接临水）
      const riverPts = lines.filter(l => l.style === 'river').flatMap(l => samplePath(l.pts, 10));
      const byRiver = p => riverPts.some(q => dist(p, q) < 0.075);
      const runs = [];
      let cur = [];
      const start = ring.findIndex(p => byRiver(p));
      const order = start < 0 ? ring : ring.slice(start).concat(ring.slice(0, start));
      for (const p of order) {
        if (byRiver(p)) { if (cur.length > 1) runs.push(cur); cur = []; } else cur.push(p);
      }
      if (cur.length > 1) runs.push(cur);
      const wallStroke = start < 0 ? [wallD] : runs.map(r => curve(r.map(T)));
      for (const d of wallStroke) g += `<path d="${d}" fill="none" stroke="${C.wall}" stroke-width="${uk(0.022)}" stroke-linejoin="round" stroke-linecap="round"/>`;
      for (const d of wallStroke) g += `<path d="${d}" fill="none" stroke="${C.tower}" stroke-width="${uk(0.008)}" stroke-linejoin="round" stroke-linecap="round"/>`;
      ring.forEach((p, i) => {
        if (i % 3 || byRiver(p)) return;
        const [tx, ty] = T(p);
        g += `<circle cx="${f1(tx)}" cy="${f1(ty)}" r="${uk(0.016)}" fill="${C.tower}" stroke="${C.wall}" stroke-width="${uk(0.005)}"/>`;
      });
      for (const n of layer.nodes.filter(n => n.kind === 'gate')) {
        // 城门：沿墙方向两座门楼
        let k = 0, bd = Infinity;
        ring.forEach((p, i) => { const d = dist(p, [n.x, n.y]); if (d < bd) { bd = d; k = i; } });
        const a = ring[(k - 1 + ring.length) % ring.length], b = ring[(k + 1) % ring.length];
        const L = dist(a, b) || 1, dx = (b[0] - a[0]) / L, dy = (b[1] - a[1]) / L;
        for (const s of [-1, 1]) {
          const [gx, gy] = T([n.x + dx * s * 0.022, n.y + dy * s * 0.022]);
          g += `<rect x="${f1(gx - uk(0.016))}" y="${f1(gy - uk(0.016))}" width="${uk(0.032)}" height="${uk(0.032)}" fill="${C.tower}" stroke="${C.wall}" stroke-width="${uk(0.005)}"/>`;
        }
        const [cx, cy] = T([n.x, n.y]);
        g += `<circle cx="${f1(cx)}" cy="${f1(cy)}" r="${uk(0.012)}" fill="${C.street}"/>`;
      }
      // 地标
      for (const n of layer.nodes.filter(n => n.landmark)) {
        const [lx, ly] = T([n.x, n.y]);
        if (n.landmark === 'dome')
          g += `<circle cx="${f1(lx)}" cy="${f1(ly)}" r="${uk(0.03)}" fill="#f6f1e6" stroke="${C.ink}" stroke-width="${uk(0.004)}"/>` +
               `<circle cx="${f1(lx)}" cy="${f1(ly)}" r="${uk(0.018)}" fill="none" stroke="${C.soft}" stroke-width="${uk(0.003)}"/>`;
        if (n.landmark === 'tower')
          g += `<circle cx="${f1(lx)}" cy="${f1(ly)}" r="${uk(0.016)}" fill="#cfc3ad" stroke="${C.ink}" stroke-width="${uk(0.005)}"/>` +
               `<circle cx="${f1(lx)}" cy="${f1(ly)}" r="${uk(0.006)}" fill="${C.ink}"/>`;
      }
    } else {
      // 没有城墙的图：街道也照大路画
      for (const l of lines.filter(l => ['street', 'lane', 'quay'].includes(l.style)))
        g += `<path d="${l.d}" fill="none" stroke="${C.casing}" stroke-width="${u(roadW[0])}"/><path d="${l.d}" fill="none" stroke="${C.street}" stroke-width="${u(roadW[1])}"/>`;
    }

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    svg.setAttribute('viewBox', vb.join(' '));
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.innerHTML = g;
    return { svg, bounds: [[y0, x0], [y1, x1]], smooth: pts => samplePath(pts, 8) };
  }

  window.AtlasArt = { draw, samplePath };
})();
