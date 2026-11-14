/**
 * Hero dot matrix: an abstract grid where a few accent dots light up in turn,
 * like engines being queried one at a time. Pauses off screen and when the tab
 * is hidden; draws one still frame under prefers-reduced-motion.
 */

const SPACING = 22;
const PULSE_MS = 1700;
const STEP_MS = 1150;

function seeded(seed) {
  let x = seed >>> 0;
  return () => { x = (x * 1664525 + 1013904223) >>> 0; return x / 2 ** 32; };
}

function readColor(node, name, fallback) {
  const value = getComputedStyle(node).getPropertyValue(name).trim();
  return value || fallback;
}

export function startDots(canvas, {reducedMotion = false} = {}) {
  const context = canvas.getContext('2d');
  if (!context) return () => {};
  const base = document.createElement('canvas');
  let width = 0, height = 0, ratio = 1, nodes = [], frame = 0, visible = false, startedAt = 0;
  const accent = readColor(canvas, '--dot-accent', '#e8aa8b');
  const dim = readColor(canvas, '--dot-base', 'rgba(255,255,255,0.13)');

  function layout() {
    const rect = canvas.getBoundingClientRect();
    ratio = Math.min(window.devicePixelRatio || 1, 2);
    width = Math.max(1, Math.round(rect.width)); height = Math.max(1, Math.round(rect.height));
    for (const target of [canvas, base]) { target.width = width * ratio; target.height = height * ratio; }
    const b = base.getContext('2d');
    b.setTransform(ratio, 0, 0, ratio, 0, 0);
    b.clearRect(0, 0, width, height);
    b.fillStyle = dim;
    const cols = Math.ceil(width / SPACING), rows = Math.ceil(height / SPACING);
    for (let r = 0; r <= rows; r += 1) {
      for (let c = 0; c <= cols; c += 1) {
        // A soft vignette: dots thin out toward the left, where the headline sits.
        const fade = Math.min(1, 0.25 + (c / cols) * 1.1);
        b.globalAlpha = fade;
        b.fillRect(c * SPACING + SPACING / 2 - 0.9, r * SPACING + SPACING / 2 - 0.9, 1.8, 1.8);
      }
    }
    b.globalAlpha = 1;
    const random = seeded(14);
    nodes = Array.from({length: 9}, () => ({
      c: Math.floor(cols * (0.42 + random() * 0.56)),
      r: Math.floor(rows * (0.12 + random() * 0.76)),
    }));
    draw(performance.now());
  }

  function glow(x, y, strength) {
    const radius = 4 + strength * 18;
    const gradient = context.createRadialGradient(x, y, 0, x, y, radius);
    gradient.addColorStop(0, accent);
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    context.globalAlpha = 0.55 * strength;
    context.fillStyle = gradient;
    context.beginPath(); context.arc(x, y, radius, 0, Math.PI * 2); context.fill();
    context.globalAlpha = 1;
    context.fillStyle = accent;
    context.beginPath(); context.arc(x, y, 1.6 + strength * 1.6, 0, Math.PI * 2); context.fill();
  }

  function draw(now) {
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(base, 0, 0);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const elapsed = reducedMotion ? 0 : now - startedAt;
    nodes.forEach((node, index) => {
      const x = node.c * SPACING + SPACING / 2, y = node.r * SPACING + SPACING / 2;
      const phase = ((elapsed - index * STEP_MS) % (STEP_MS * nodes.length) + STEP_MS * nodes.length) % (STEP_MS * nodes.length);
      const strength = reducedMotion ? (index % 3 === 0 ? 0.8 : 0.25) : phase < PULSE_MS ? Math.sin((phase / PULSE_MS) * Math.PI) : 0;
      // Resting engines stay faintly lit so the field reads as "engines on standby".
      glow(x, y, Math.max(0.18, strength));
      if (strength > 0.05 && !reducedMotion) {
        // A ripple through neighbouring grid dots.
        const ring = (phase / PULSE_MS) * SPACING * 5;
        context.fillStyle = accent;
        for (let dr = -6; dr <= 6; dr += 1) {
          for (let dc = -6; dc <= 6; dc += 1) {
            const dx = dc * SPACING, dy = dr * SPACING, d = Math.hypot(dx, dy);
            const near = 1 - Math.abs(d - ring) / SPACING;
            if (near <= 0 || d === 0) continue;
            context.globalAlpha = near * strength * 0.6;
            context.fillRect(x + dx - 1, y + dy - 1, 2, 2);
          }
        }
        context.globalAlpha = 1;
      }
    });
  }

  function loop(now) {
    draw(now);
    frame = visible && !document.hidden && !reducedMotion ? requestAnimationFrame(loop) : 0;
  }
  function wake() {
    if (!frame && visible && !document.hidden && !reducedMotion) frame = requestAnimationFrame(loop);
  }

  startedAt = performance.now();
  new ResizeObserver(layout).observe(canvas);
  new IntersectionObserver(entries => { visible = entries[0].isIntersecting; wake(); }).observe(canvas);
  document.addEventListener('visibilitychange', wake);
  return () => { visible = false; cancelAnimationFrame(frame); frame = 0; };
}
