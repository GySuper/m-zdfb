// 新版界面(Auto)的通用交互:丝绸背景、数字滚动、3D 倾斜、磁吸按钮、平台切换、液体水位、操作错误弹窗。
(() => {
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // 丝绸:canvas[data-silk]
  $$('canvas[data-silk]').forEach((c) => {
    const g = c.getContext('2d');
    const cols = [[124, 58, 237], [236, 72, 153], [59, 130, 246], [249, 115, 22], [16, 185, 129]];
    const size = () => { c.width = c.offsetWidth * devicePixelRatio; c.height = c.offsetHeight * devicePixelRatio; };
    size(); addEventListener('resize', size);
    let visible = true;
    new IntersectionObserver(([e]) => { visible = e.isIntersecting; }).observe(c);
    const draw = (t) => {
      const W = c.width, H = c.height, bg = g.createLinearGradient(0, 0, W, H);
      bg.addColorStop(0, '#3b0764'); bg.addColorStop(0.5, '#6d28d9'); bg.addColorStop(1, '#db2777');
      g.fillStyle = bg; g.fillRect(0, 0, W, H);
      g.globalCompositeOperation = 'screen';
      for (let k = 0; k < 26; k++) {
        const col = cols[k % cols.length];
        g.beginPath();
        for (let x = 0; x <= W; x += 12) {
          const y = H * 0.55 + Math.sin(x * 0.0035 + t * 0.0005 + k * 0.25) * H * 0.22 + Math.sin(x * 0.009 - t * 0.0008 + k) * H * 0.05 + (k - 13) * 6 * devicePixelRatio;
          x ? g.lineTo(x, y) : g.moveTo(x, y);
        }
        g.strokeStyle = `rgba(${col},${0.1 + 0.05 * Math.sin(t * 0.001 + k)})`;
        g.lineWidth = (2 + (k % 4)) * devicePixelRatio; g.stroke();
      }
      g.globalCompositeOperation = 'source-over';
    };
    // 始终流动,不跟随系统「减少动画」:办公电脑多为省性能关掉系统动画,静止的横幅不好看(用户 2026-11-20 定)
    const loop = (t) => { if (visible) draw(t); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  });

  // 数字滚动:[data-count]
  $$('[data-count]').forEach((el) => {
    const to = +el.dataset.count, t0 = performance.now(), ms = 1400;
    if (reduce || !to) { el.textContent = to; return; }
    const f = (t) => { const p = Math.min(1, (t - t0) / ms); el.textContent = Math.round(to * (1 - Math.pow(1 - p, 4))); if (p < 1) requestAnimationFrame(f); };
    requestAnimationFrame(f);
  });

  // 液体水位:[data-fill]
  setTimeout(() => $$('[data-fill]').forEach((el) => { el.style.height = el.dataset.fill + '%'; }), 300);

  // 3D 倾斜:.tilt
  if (!reduce) $$('.tilt').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      const r = el.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
      el.style.transform = `perspective(900px) rotateY(${x * 8}deg) rotateX(${-y * 8}deg) translateY(-4px)`;
    });
    el.addEventListener('mouseleave', () => { el.style.transform = ''; });
  });

  // 磁吸:[data-magnet] 包裹一个 .mag 按钮
  if (!reduce) $$('[data-magnet]').forEach((wrap) => {
    const b = wrap.querySelector('.mag');
    wrap.addEventListener('mousemove', (e) => { const r = b.getBoundingClientRect(); b.style.transform = `translate(${(e.clientX - r.left - r.width / 2) * 0.25}px,${(e.clientY - r.top - r.height / 2) * 0.25}px) scale(1.04)`; });
    wrap.addEventListener('mouseleave', () => { b.style.transform = ''; });
  });

  // 平台切换
  const btn = document.getElementById('pfBtn'), menu = document.getElementById('pfMenu'), chev = document.getElementById('pfChev');
  if (btn && menu) {
    const set = (open) => { menu.hidden = !open; btn.setAttribute('aria-expanded', String(open)); if (chev) chev.style.transform = open ? 'rotate(180deg)' : ''; };
    btn.addEventListener('click', (e) => { e.stopPropagation(); set(menu.hidden); });
    document.addEventListener('click', (e) => { if (!menu.contains(e.target)) set(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') set(false); });
  }

  // 锁定平台(未完成插件化改造):点击只提示没权限,不跳转
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-locked]');
    if (!b) return;
    document.body.dispatchEvent(new CustomEvent('opError', {
      detail: { title: '没权限', detail: `${b.dataset.locked}${b.dataset.hint},请先用已开放的平台。` },
    }));
  });

  // 操作失败弹窗:后端 HX-Trigger: {"opError": {"title","detail"}}
  document.body.addEventListener('opError', (e) => {
    const d = e.detail || {};
    const m = document.getElementById('op-modal');
    document.getElementById('op-modal-title').textContent = d.title || '操作失败';
    document.getElementById('op-modal-detail').textContent = d.detail || d.value || '';
    if (m && m.showModal) m.showModal();
  });
})();
