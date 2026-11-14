/**
 * Page dot field: one fixed grid of dots behind every section, so the page reads
 * as a single surface. While the hero is on screen a few accent dots light up in
 * turn, like engines being queried one at a time; they fade out as the hero
 * scrolls away, leaving the plain grid behind the rest of the page. Pauses when
 * the hero is off screen or the tab is hidden; draws one still frame under
 * prefers-reduced-motion.
 */

const SPACING = 22;
const PULSE_MS = 1700;
const STEP_MS = 1150;
const NARROW = 720;

function seeded(seed) {
  let x = seed >>> 0;
  return () => { x = (x * 1664525 + 1013904223) >>> 0; return x / 2 ** 32; };
}

function readColor(node, name, fallback) {
  const value = getComputedStyle(node).getPropertyValue(name).trim();
  return value || fallback;
}

export function startDots(canvas, {hero = null, reducedMotion = false} = {}) {
  const context = canvas.getContext('2d');
  if (!context) return () => {};
  const base = document.createElement('canvas');
  let width = 0, height = 0, ratio = 1, nodes = [], frame = 0, startedAt = 0;
  const accent = readColor(canvas, '--dot-accent', '#a3432c');
  const dim = readColor(canvas, '--dot-base', 'rgba(38,40,33,0.17)');

  // How much of the hero is still on screen, 0 to 1. Accent dots follow it.
  function heroShare() {
    if (!hero) return 0;
    const rect = hero.getBoundingClientRect();
    return Math.max(0, Math.min(1, rect.bottom / Math.max(1, rect.height)));
  }

  function layout() {
    ratio = Math.min(window.devicePixelRatio || 1, 2);
    width = Math.max(1, Math.round(canvas.clientWidth)); height = Math.max(1, Math.round(canvas.clientHeight));
    for (const target of [canvas, base]) { target.width = width * ratio; target.height = height * ratio; }
    const b = base.getContext('2d');
    b.setTransform(ratio, 0, 0, ratio, 0, 0);
    b.clearRect(0, 0, width, height);
    b.fillStyle = dim;
    const cols = Math.ceil(width / SPACING), rows = Math.ceil(height / SPACING);
    for (let r = 0; r <= rows; r += 1) {
      for (let c = 0; c <= cols; c += 1) {
        // Dots thin out toward the left, where headlines and body text sit.
        b.globalAlpha = Math.min(1, 0.4 + (c / cols) * 0.9);
        b.fillRect(c * SPACING + SPACING / 2 - 0.9, r * SPACING + SPACING / 2 - 0.9, 1.8, 1.8);
      }
    }
    b.globalAlpha = 1;
    // Accent dots sit in the right part of the hero, as it is laid out at the top of the page.
    const heroRows = Math.min(rows, Math.ceil((hero ? hero.offsetHeight + hero.offsetTop : height) / SPACING));
    const random = seeded(14);
    nodes = Array.from({length: 9}, () => ({
      c: Math.floor(cols * (0.5 + random() * 0.48)),
      r: Math.floor(heroRows * (0.16 + random() * 0.72)),
    }));
    draw(performance.now());
  }

  function glow(x, y, strength, share) {
    const radius = 4 + strength * 16;
    const gradient = context.createRadialGradient(x, y, 0, x, y, radius);
    gradient.addColorStop(0, accent);
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    context.globalAlpha = 0.3 * strength * share;
    context.fillStyle = gradient;
    context.beginPath(); context.arc(x, y, radius, 0, Math.PI * 2); context.fill();
    context.globalAlpha = share;
    context.fillStyle = accent;
    context.beginPath(); context.arc(x, y, 1.5 + strength * 1.4, 0, Math.PI * 2); context.fill();
    context.globalAlpha = 1;
  }

  function draw(now) {
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(base, 0, 0);
    // On a narrow screen the hero text spans the full width, so the accent dots would sit behind it.
    const share = width < NARROW ? 0 : heroShare();
    if (share <= 0) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const elapsed = reducedMotion ? 0 : now - startedAt;
    const cycle = STEP_MS * nodes.length;
    nodes.forEach((node, index) => {
      const x = node.c * SPACING + SPACING / 2, y = node.r * SPACING + SPACING / 2;
      const phase = (((elapsed - index * STEP_MS) % cycle) + cycle) % cycle;
      const strength = reducedMotion ? (index % 3 === 0 ? 0.8 : 0.25) : phase < PULSE_MS ? Math.sin((phase / PULSE_MS) * Math.PI) : 0;
      // Resting engines stay faintly lit so the field reads as "engines on standby".
      glow(x, y, Math.max(0.18, strength), share);
      if (strength > 0.05 && !reducedMotion) {
        // A ripple through neighbouring grid dots.
        const ring = (phase / PULSE_MS) * SPACING * 5;
        context.fillStyle = accent;
        for (let dr = -6; dr <= 6; dr += 1) {
          for (let dc = -6; dc <= 6; dc += 1) {
            const dx = dc * SPACING, dy = dr * SPACING, d = Math.hypot(dx, dy);
            const near = 1 - Math.abs(d - ring) / SPACING;
            if (near <= 0 || d === 0) continue;
            context.globalAlpha = near * strength * 0.45 * share;
            context.fillRect(x + dx - 1, y + dy - 1, 2, 2);
          }
        }
        context.globalAlpha = 1;
      }
    });
  }

  const running = () => !reducedMotion && !document.hidden && width >= NARROW && heroShare() > 0;
  function loop(now) {
    draw(now);
    frame = running() ? requestAnimationFrame(loop) : 0;
  }
  function wake() {
    if (frame) return;
    if (running()) frame = requestAnimationFrame(loop);
    else draw(performance.now());  // one resting frame: the plain grid, or the still hero under reduced motion
  }

  startedAt = performance.now();
  new ResizeObserver(layout).observe(canvas);
  if (hero) new IntersectionObserver(wake, {threshold: [0, 0.25, 0.5, 0.75, 1]}).observe(hero);
  addEventListener('scroll', () => { if (!frame) wake(); }, {passive: true});
  document.addEventListener('visibilitychange', wake);
  return () => { reducedMotion = true; cancelAnimationFrame(frame); frame = 0; };
}
