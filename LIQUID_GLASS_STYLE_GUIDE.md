# Liquid Glass Design System v2 — Style Guide
### uDispute by Bergomy Legendre

---

## Design Philosophy

uDispute is built against every sterile, corporate software UI on the market. The **Liquid Glass Design System** uses transparency as a metaphor — glass you can see through, because credit repair should have nothing to hide. Every surface is frosted, every edge catches light, every interaction feels physical. The "u" in uDispute means **you** — the user is always in control.

---

## 1. Color Tokens

### Glass Surfaces
| Token | Value | Usage |
|-------|-------|-------|
| `--glass-bg` | `rgba(255, 255, 255, 0.07)` | Standard glass layer |
| `--glass-bg-heavy` | `rgba(255, 255, 255, 0.10)` | Heavier, more opaque glass |
| `--glass-bg-light` | `rgba(255, 255, 255, 0.04)` | Ultra-light, minimal opacity |

### Glass Borders
| Token | Value | Usage |
|-------|-------|-------|
| `--glass-border` | `rgba(255, 255, 255, 0.35)` | Standard border light catch |
| `--glass-border-subtle` | `rgba(255, 255, 255, 0.18)` | Subtle edge definition |
| `--glass-border-bright` | `rgba(255, 255, 255, 0.55)` | Bright reflective edge |

### Glass Shadows
| Token | Value | Usage |
|-------|-------|-------|
| `--glass-shadow` | `rgba(0, 0, 0, 0.08)` | Standard depth shadow |
| `--glass-shadow-hover` | `rgba(0, 0, 0, 0.14)` | Elevated hover shadow |
| `--glass-shadow-deep` | `rgba(0, 0, 0, 0.20)` | Deep shadow for high elevation |

### Specular / Reflection
| Token | Value |
|-------|-------|
| `--glass-specular` | `rgba(255, 255, 255, 0.5)` |
| `--glass-specular-soft` | `rgba(255, 255, 255, 0.25)` |
| `--glass-reflection` | `linear-gradient(135deg, rgba(255,255,255,0.35) 0%, rgba(255,255,255,0.05) 40%, rgba(255,255,255,0.0) 50%, rgba(255,255,255,0.08) 80%, rgba(255,255,255,0.20) 100%)` |
| `--glass-edge-light` | `linear-gradient(180deg, rgba(255,255,255,0.6) 0%, rgba(255,255,255,0.0) 50%, rgba(255,255,255,0.15) 100%)` |

### Text Colors
| Token | Value | Usage |
|-------|-------|-------|
| `--text-primary` | `#1d1d1f` | Body text, headings |
| `--text-secondary` | `#48484a` | Secondary text, labels |
| `--text-tertiary` | `#86868b` | Tertiary, disabled, hints |

### Accent Colors
| Token | Value | Usage |
|-------|-------|-------|
| `--accent` | `#0071e3` | Primary brand blue |
| `--accent-hover` | `#0077ed` | Hover state blue |
| `--accent-light` | `rgba(0, 113, 227, 0.12)` | Faint blue background |
| `--accent-glow` | `rgba(0, 113, 227, 0.30)` | Blue glow for shadows |

### Semantic Colors
| Color | Hex | Usage |
|-------|-----|-------|
| Blue | `#0071e3` | Primary brand |
| Red | `#ff3b30` | Error / danger |
| Green | `#34c759` | Success |
| Yellow | `#ff9f0a` | Warning |
| Purple | `#af52de` | Secondary accent |
| Pink | `#ff2d55` | Tertiary accent |

---

## 2. Typography

### Font Stack
```css
--font-display: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
--font-body: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
--font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
```

### Scale
| Element | Size | Weight | Tracking |
|---------|------|--------|----------|
| h1 | — | 800 | -0.03em |
| h2 | — | 700 | -0.025em |
| h3 | — | 700 | -0.02em |
| Body (p) | — | — | — (line-height: 1.7) |
| Button/Label | 0.9375rem (15px) | 600 | — |
| Form Label | 0.875rem (14px) | 600 | — |
| Tab | 0.875rem (14px) | 500 (600 active) | — |
| Metric Value | 2.25rem (36px) | 800 | -0.03em |
| Metric Label | 0.7rem (11.2px) | 700 | 0.08em (uppercase) |
| Badge | 0.7rem (11.2px) | 600 | 0.05em (uppercase) |
| Table Header | 0.75rem (12px) | 600 | 0.06em (uppercase) |

### Text Treatments
| Class | Effect |
|-------|--------|
| `.glass-text-3d` | Layered depth: `0 1px 0 rgba(255,255,255,0.5), 0 2px 2px rgba(0,0,0,0.08), 0 3px 6px rgba(0,0,0,0.06), 0 5px 10px rgba(0,0,0,0.04)` |
| `.glass-text-emboss` | Light emboss: `0 1px 1px rgba(255,255,255,0.6), 0 -1px 1px rgba(0,0,0,0.08)` |
| `.glass-text-deboss` | Pressed in: `0 -1px 1px rgba(255,255,255,0.5), 0 1px 2px rgba(0,0,0,0.12)` |
| `.glass-text-chrome` | Animated chrome gradient with drop-shadow, 10s shimmer loop |
| `.hero-3d-chrome` | Blue-purple chrome gradient, 6s shimmer loop |

---

## 3. Blur / Backdrop Filter

### Blur Tokens
| Token | Value | Usage |
|-------|-------|-------|
| `--blur-sm` | `16px` | Inputs, tabs, small elements |
| `--blur-md` | `28px` | Standard glass cards |
| `--blur-lg` | `48px` | Heavy cards, metric panels |
| `--blur-xl` | `72px` | Header, overlays |

### Backdrop Filter Chains
| Surface | Chain |
|---------|-------|
| Standard card | `blur(28px) saturate(200%) brightness(1.08)` |
| Heavy card | `blur(48px) saturate(220%) brightness(1.1)` |
| Light card | `blur(16px) saturate(170%) brightness(1.05)` |
| Header | `blur(72px) saturate(200%) brightness(1.1)` |
| Inputs | `blur(16px) saturate(180%) brightness(1.05)` |

---

## 4. Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-sm` | `16px` | Buttons, inputs, small cards |
| `--radius-md` | `22px` | Medium elements |
| `--radius-lg` | `28px` | Large cards, panels |
| `--radius-xl` | `36px` | Extra large |
| Pill | `100px` | Pill buttons, toggles, badges |
| Icon well | `14px` | Standard icon containers |
| Icon well (sm) | `11px` | Small icon containers |
| Icon well (lg) | `18px` | Large icon containers |

---

## 5. Spacing & Sizing

### Icon Wells
| Variant | Size |
|---------|------|
| Standard | 52px × 52px |
| Small (--sm) | 40px × 40px |
| Large (--lg) | 64px × 64px |

### Toggle Switch
| Variant | Dimensions | Thumb |
|---------|-----------|-------|
| Standard | 48px × 26px | 22px × 22px, translateX(22px) active |
| Small | — × 20px | 16px × 16px, translateX(17px) active |

### Padding Utilities
| Class | Value |
|-------|-------|
| `.glass-p` | 2rem (32px) |
| `.glass-p-sm` | 1.25rem (20px) |
| `.glass-p-lg` | 2.5rem (40px) |

---

## 6. Transitions & Easing

### Transition Tokens
| Token | Value | Usage |
|-------|-------|-------|
| `--transition-fast` | `0.18s cubic-bezier(0.4, 0, 0.2, 1)` | Snappy micro-interactions |
| `--transition-normal` | `0.3s cubic-bezier(0.25, 0.1, 0.25, 1)` | Standard smooth motion |
| `--transition-slow` | `0.5s cubic-bezier(0.25, 0.1, 0.25, 1)` | Deliberate, leisurely |

### Spring Curve
```css
cubic-bezier(0.22, 1, 0.36, 1)  /* Bouncy spring — used for hover lifts, card transitions */
```

### Dispute Folder Drag LERP
```
LERP = 0.18  /* 0 = frozen, 1 = rigid, 0.18 = smooth trail */
```

---

## 7. Glass Card System

### Standard Card (`.glass-card`)
```css
background: rgba(255, 255, 255, 0.07);
backdrop-filter: blur(28px) saturate(200%) brightness(1.08);
border: 1px solid rgba(255, 255, 255, 0.35);
border-radius: 28px;
box-shadow:
  0 8px 32px rgba(0,0,0,0.08),
  0 2px 8px rgba(0,0,0,0.04),
  inset 0 1px 0 rgba(255,255,255,0.5),
  inset 0 -1px 0 rgba(255,255,255,0.08);
```

### Heavy Card (`.glass-card--heavy`)
```css
background: rgba(255, 255, 255, 0.10);
backdrop-filter: blur(48px) saturate(220%) brightness(1.1);
box-shadow:
  0 12px 40px rgba(0,0,0,0.08),
  0 2px 8px rgba(0,0,0,0.05),
  inset 0 1px 0 rgba(255,255,255,0.5),
  inset 0 -1px 0 rgba(255,255,255,0.1);
```

### Light Card (`.glass-card--light`)
```css
background: rgba(255, 255, 255, 0.04);
backdrop-filter: blur(16px) saturate(170%) brightness(1.05);
border-color: rgba(255, 255, 255, 0.18);
```

### Card Pseudo-Elements
- `::before` — Specular reflection overlay (135deg diagonal gradient sweep)
- `::after` — Top edge highlight (1px horizontal light line)

### Hover State
```css
transform: translateY(-2px);
/* Enhanced shadows with 0 0 20px rgba(0,113,227,0.06) accent glow */
```

---

## 8. Neumorphic Inputs

### Text Input (`.glass-input`)
```css
padding: 0.75rem 1rem;
font-size: 0.9375rem;
background: rgba(0,0,0,0.02);
backdrop-filter: blur(16px) saturate(180%) brightness(1.05);
border: 1px solid rgba(0,0,0,0.04);
border-radius: 16px;
box-shadow:
  inset 2px 2px 5px rgba(0,0,0,0.06),
  inset -2px -2px 4px rgba(255,255,255,0.4),
  0 1px 0 rgba(255,255,255,0.3);
```

### Focus State
```css
border-color: var(--accent);
box-shadow: 0 0 0 3px rgba(0,113,227,0.12);
background: rgba(255,255,255,0.08);
```

### Select (`.glass-select`)
```css
background-color: rgba(255,255,255,0.06);
border: 1px solid rgba(255,255,255,0.25);
box-shadow:
  inset 0 2px 4px rgba(0,0,0,0.04),
  inset 0 0 0 1px rgba(255,255,255,0.05),
  0 2px 8px rgba(0,0,0,0.04);
```

---

## 9. Button System

### Primary (`.glass-btn--primary`)
```css
background: linear-gradient(135deg, #0077ed 0%, #0071e3 50%, #005bb5 100%);
color: #fff;
box-shadow:
  0 4px 16px rgba(0,113,227,0.30),
  inset 0 1px 0 rgba(255,255,255,0.25);
/* Hover: translateY(-1px), intensified glow */
/* Active: translateY(1px), inset shadow */
```

### Secondary (`.glass-btn--secondary`)
```css
background: rgba(255,255,255,0.15);
backdrop-filter: blur(16px) saturate(180%);
border: 1px solid rgba(255,255,255,0.35);
box-shadow:
  0 2px 8px rgba(0,0,0,0.08),
  inset 0 1px 0 rgba(255,255,255,0.4);
```

### Ghost (`.glass-btn--ghost`)
```css
background: transparent;
color: var(--accent);
/* Hover: background: rgba(0,113,227,0.12) */
```

### Danger (`.glass-btn--danger`)
```css
background: linear-gradient(135deg, #ff453a 0%, #ff3b30 50%, #d70015 100%);
box-shadow: 0 4px 16px rgba(255,59,48,0.30);
```

### Size Variants
| Variant | Padding | Radius |
|---------|---------|--------|
| Standard | `0.65rem 1.5rem` | 16px |
| Pill (`--pill`) | `0.55rem 1.4rem` | 100px |
| Large (`--lg`) | `0.85rem 2rem` | 16px, font: 17px |
| Full (`--full`) | `0.85rem` (width: 100%) | 16px |

---

## 10. Metric Cards (`.glass-metric`)

```css
background: rgba(255,255,255,0.22);
backdrop-filter: blur(48px) saturate(200%) brightness(1.1);
border: 1px solid rgba(255,255,255,0.45);
border-radius: 28px;
padding: 1.75rem 1.5rem;
box-shadow:
  0 12px 40px rgba(0,0,0,0.12),
  0 4px 12px rgba(0,0,0,0.06),
  inset 0 1px 0 rgba(255,255,255,0.6),
  inset 0 -1px 0 rgba(255,255,255,0.1);
transform: translateY(-2px); /* resting lift */
```

### Color Underglow (::after)
Each metric variant gets a colored underglow blur at the bottom:
- Blue: `#0071e3`, Red: `#ff3b30`, Green: `#34c759`
- Yellow: `#ff9f0a`, Purple: `#af52de`, Pink: `#ff2d55`
- Filter: `blur(14px)`, opacity: `0.5` → `0.8` on hover

---

## 11. Badges (`.glass-badge`)

```css
padding: 0.3rem 0.85rem;
font-size: 0.7rem;
font-weight: 600;
text-transform: uppercase;
letter-spacing: 0.05em;
border-radius: 16px;
backdrop-filter: blur(12px) saturate(180%);
```

| Variant | Background | Color | Border |
|---------|-----------|-------|--------|
| `--success` | `rgba(52,199,89,0.10)` | `#2da44e` | `rgba(52,199,89,0.22)` |
| `--danger` | `rgba(255,59,48,0.08)` | `#e5484d` | `rgba(255,59,48,0.18)` |
| `--info` | `rgba(0,113,227,0.08)` | `#3b82f6` | `rgba(0,113,227,0.18)` |
| `--warning` | `rgba(255,159,10,0.10)` | `#d97706` | `rgba(255,159,10,0.22)` |
| `--neutral` | `rgba(142,142,147,0.08)` | `#6e6e73` | `rgba(142,142,147,0.18)` |

---

## 12. Neumorphic Primitives

### Icon Well (`.glass-icon-well`) — Recessed
```css
background: linear-gradient(145deg, rgba(0,0,0,0.03), rgba(255,255,255,0.1));
border: 1px solid rgba(255,255,255,0.2);
box-shadow:
  inset 3px 3px 6px rgba(0,0,0,0.08),
  inset -2px -2px 5px rgba(255,255,255,0.5),
  0 1px 2px rgba(0,0,0,0.03);
```

### Icon Raised (`.glass-icon-raised`) — Elevated
```css
background: linear-gradient(135deg, rgba(255,255,255,0.3) 0%, rgba(255,255,255,0.1) 100%);
backdrop-filter: blur(8px);
border: 1px solid rgba(255,255,255,0.4);
box-shadow:
  0 4px 12px rgba(0,0,0,0.1),
  0 1px 3px rgba(0,0,0,0.06),
  inset 0 1px 0 rgba(255,255,255,0.6),
  inset 0 -1px 0 rgba(0,0,0,0.04);
```

### Inset (`.glass-inset`) — Pressed surface
```css
background: rgba(0,0,0,0.02);
border: 1px solid rgba(0,0,0,0.03);
box-shadow:
  inset 2px 2px 5px rgba(0,0,0,0.07),
  inset -2px -2px 4px rgba(255,255,255,0.45);
```

### Raised (`.glass-raised`) — Floating above
```css
box-shadow:
  4px 4px 12px rgba(0,0,0,0.08),
  -2px -2px 8px rgba(255,255,255,0.45),
  inset 0 1px 0 rgba(255,255,255,0.5);
```

---

## 13. Animations & Effects

### Page Enter (`.glass-animate-in`)
```css
animation: glass-fade-in 0.6s cubic-bezier(0.25, 0.1, 0.25, 1) both;
/* from: opacity:0, translateY(16px) scale(0.98) → to: opacity:1, translateY(0) scale(1) */
```

### Stagger (`.glass-stagger > *`)
Delays: 0.06s, 0.12s, 0.18s, 0.24s, 0.30s, 0.36s per child

### Shimmer Edge (`.glass-shimmer-edge`)
```css
/* 2px light line sweeps left-to-right across the top edge */
animation: glass-shimmer-sweep 8s ease-in-out infinite;
/* gradient: transparent → rgba(255,255,255,0.3) → 0.6 → 0.3 → transparent */
```

### Pulse Glow (`.glass-pulse`)
```css
animation: glass-pulse-glow 2.5s ease-in-out infinite;
/* 0%,100%: 0 0 8px rgba(0,113,227,0.3) → 50%: 0 0 20px rgba(0,113,227,0.6) */
```

### Breathe (`.glass-breathe`)
```css
animation: glass-breathe 4s ease-in-out infinite;
/* Subtle scale(1) → scale(1.006) with shadow intensity shift */
```

### Chrome Shimmer
```css
animation: chrome-shimmer 10s ease-in-out infinite;
/* background-position: 0% 50% → 100% 50% → 0% 50% */
```

### Hover Lift (`.glass-hover-lift`)
```css
/* Hover: translateY(-4px), box-shadow: 0 12px 40px rgba(0,0,0,0.15) */
transition: transform 0.3s ease, box-shadow 0.3s ease;
```

---

## 14. Status Dots (`.glass-dot`)

```css
width: 10px; height: 10px; border-radius: 50%;
box-shadow: inset 1px 1px 2px rgba(255,255,255,0.5), 0 2px 4px rgba(0,0,0,0.12);
```

| Variant | Gradient | Glow |
|---------|----------|------|
| `--active` (green) | `radial-gradient(circle at 35% 35%, #5bef7f, #34c759)` | `0 0 8px rgba(52,199,89,0.4)` |
| `--warning` (yellow) | `radial-gradient(circle at 35% 35%, #ffcb47, #ff9f0a)` | `0 0 8px rgba(255,159,10,0.4)` |
| `--danger` (red) | `radial-gradient(circle at 35% 35%, #ff7b73, #ff3b30)` | `0 0 8px rgba(255,59,48,0.4)` |
| `--blue` | `radial-gradient(circle at 35% 35%, #4da3ff, #0071e3)` | `0 0 8px rgba(0,113,227,0.4)` |
| `--inactive` (gray) | `radial-gradient(circle at 35% 35%, #c7c7cc, #8e8e93)` | `0 1px 3px rgba(0,0,0,0.08)` |

---

## 15. Tabs (`.glass-tabs`)

### Container
```css
display: flex; gap: 0.25rem; padding: 0.3rem;
background: rgba(255,255,255,0.08);
backdrop-filter: blur(16px) saturate(180%);
border-radius: 16px;
border: 1px solid rgba(255,255,255,0.18);
```

### Active Tab
```css
background: rgba(255,255,255,0.25);
font-weight: 600;
box-shadow: 0 2px 8px rgba(0,0,0,0.06), inset 0 1px 0 rgba(255,255,255,0.4);
```

---

## 16. Dispute Folder Window

### Container
```css
position: fixed;
width: 560px; max-width: 94vw; max-height: 82vh;
background: rgba(255,255,255,0.13);
backdrop-filter: blur(32px) saturate(190%) brightness(1.06);
border: 1px solid rgba(255,255,255,0.30);
border-radius: 16px;
box-shadow:
  0 24px 80px rgba(0,0,0,0.18),
  0 8px 28px rgba(0,0,0,0.10),
  0 2px 8px rgba(0,0,0,0.06),
  inset 0 1px 0 rgba(255,255,255,0.50),
  inset 0 -1px 0 rgba(255,255,255,0.06);
z-index: 200;
```

### Titlebar
```css
padding: 0.6rem 1rem 0.55rem 0.85rem;
background: rgba(255,255,255,0.08);
border-bottom: 1px solid rgba(255,255,255,0.12);
cursor: grab; /* grabbing on mousedown */
```

### Close Button (macOS traffic light)
```css
width: 14px; height: 14px; border-radius: 50%;
background: linear-gradient(135deg, #ff6058, #e0443e);
border: 1px solid rgba(0,0,0,0.12);
/* Hover: glow 0 0 6px rgba(255,96,88,0.4) */
```

### Drag Behavior
- LERP interpolation: `0.18` (smooth trailing motion)
- Pixel-based positioning from open (no percentage values)
- Enhanced shadow while dragging (`.is-dragging`)

### Edge Shimmer (4 edges)
```
Top:    6s ease-in-out infinite       — rgba(255,255,255,0.45)
Bottom: 6s ease-in-out 3s infinite    — rgba(255,255,255,0.3)
Left:   7s ease-in-out 1.5s infinite  — rgba(255,255,255,0.35)
Right:  7s ease-in-out 4.5s infinite  — rgba(255,255,255,0.35)
```

---

## 17. Background

### Body (`.glass-body`)
```css
/* 4 radial gradient layers + 1 linear base */
radial-gradient(ellipse at 20% 50%, rgba(120,160,230,0.35) 0%, transparent 60%),
radial-gradient(ellipse at 80% 20%, rgba(200,140,220,0.3) 0%, transparent 55%),
radial-gradient(ellipse at 60% 80%, rgba(100,200,180,0.25) 0%, transparent 55%),
radial-gradient(ellipse at 30% 10%, rgba(255,180,130,0.2) 0%, transparent 50%),
linear-gradient(135deg, #e2e8f4 0%, #d0d8e8 30%, #c5cfe0 50%, #d6ddef 70%, #e8ddf0 100%);
background-attachment: fixed;
```

### Animated Variant (`.glass-body--animated`)
```css
/* background-size: 300% 300% on gradient layer */
animation: glass-gradient-shift 25s ease infinite;
```

---

## 18. SVG Frosted Filter

### Standard (`#frosted`)
```
feGaussianBlur: stdDeviation 18
feColorMatrix: saturate 1.8
feComponentTransfer: slope 1.08, intercept 0.02
feSpecularLighting: surfaceScale 4, specularConstant 0.6, specularExponent 30
  fePointLight: x:200, y:-100, z:300
feBlend: mode screen
```

---

## 19. Header

```css
background: rgba(230, 232, 240, 0.78);
backdrop-filter: blur(72px) saturate(200%) brightness(1.1);
border-bottom: 1px solid rgba(255, 255, 255, 0.35);
box-shadow:
  0 4px 24px rgba(0,0,0,0.08),
  0 1px 6px rgba(0,0,0,0.04),
  inset 0 -1px 0 rgba(255,255,255,0.25),
  inset 0 1px 0 rgba(255,255,255,0.5);
```

---

## 20. Accessibility

### Reduced Motion
```css
@media (prefers-reduced-motion: reduce) {
  .glass-text-chrome,
  .hero-3d-chrome,
  .glass-shimmer-edge::after,
  .glass-breathe,
  .glass-animate-in--pending {
    animation: none;
  }
}
```

---

## 21. Landing Page Specifics

### Hero Entrance
| Element | Animation | Delay | Duration |
|---------|-----------|-------|----------|
| Badge | `hero-badge-in` | 0.15s | 1.1s |
| Title | `hero-slide-up` | 0.3s | 1.2s |
| Subtitle | `hero-slide-up` | 0.5s | 1.2s |
| CTAs | `hero-cta-in` | 0.7s | 1.2s |

All use `cubic-bezier(0.22, 1, 0.36, 1)` (spring curve).

### Background Orbs
| Orb | Size | Duration | Movement |
|-----|------|----------|----------|
| 1 | 700px | 18s | translate(80px, 50px) scale(1.15) |
| 2 | 500px | 22s | translate(-70px, 40px) scale(1.2) |
| 3 | 400px | 20s | translate(50px, -40px) scale(1.12) |

### Scroll Reveal
```css
.reveal {
  opacity: 0; transform: translateY(40px) scale(0.98); filter: blur(3px);
  transition: all 1s cubic-bezier(0.22, 1, 0.36, 1);
}
.reveal.visible {
  opacity: 1; transform: none; filter: none;
}
/* Stagger: 0s, 0.1s, 0.2s, 0.3s, 0.4s, 0.5s per child */
```

### Pricing Cards
```css
box-shadow:
  4px 4px 16px rgba(0,0,0,0.08),
  -2px -2px 10px rgba(255,255,255,0.35),
  inset 0 1px 0 rgba(255,255,255,0.4);
/* Hover: translateY(-8px) with intensified shadows */
```

---

## Quick Reference — Design Rules

1. **Every surface is glass.** No opaque backgrounds except the body gradient.
2. **Inset = recessed.** Outset = elevated. This is the neumorphic depth language.
3. **White inset top border, subtle bottom border.** Always. This sells the 3D glass.
4. **`cubic-bezier(0.22, 1, 0.36, 1)`** is the spring curve. Use it for anything that should feel physical.
5. **Specular highlights on everything.** `::before` pseudo-elements carry the diagonal reflection.
6. **Edge highlights on top.** `::after` pseudo-elements carry the 1px light line.
7. **Accent blue (`#0071e3`) is the only brand color.** Everything else is semantic.
8. **Uppercase tracking for labels.** `letter-spacing: 0.06em–0.08em` with `text-transform: uppercase`.
9. **Shadows always have 2–3 layers.** Ambient + direct + optional accent glow.
10. **Reduced motion support.** Every animation has a `prefers-reduced-motion` fallback.
