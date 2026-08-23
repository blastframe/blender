# How Blender Draws Grease Pencil Curve Types

### A mathematical and code-level walkthrough of Poly, Catmull-Rom, Bézier and NURBS strokes

*Target tree:* `blastframe/blender`, branch `arena/01a02e8a-blender`, base commit `d13bbc11` (Blender 5.03 alpha, `BLENDER_VERSION 503`).
All line numbers refer to that checkout.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [The data model: where a Grease Pencil stroke actually lives](#2-the-data-model-where-a-grease-pencil-stroke-actually-lives)
3. [The evaluation pipeline](#3-the-evaluation-pipeline)
4. [Poly curves](#4-poly-curves)
5. [Catmull-Rom curves](#5-catmull-rom-curves)
6. [Bézier curves](#6-bézier-curves)
7. [NURBS curves](#7-nurbs-curves)
8. [Tangents and normals (shared by all types)](#8-tangents-and-normals-shared-by-all-types)
9. [From evaluated points to pixels: the Grease Pencil draw path](#9-from-evaluated-points-to-pixels-the-grease-pencil-draw-path)
10. [Edit-mode overlay drawing, per curve type](#10-edit-mode-overlay-drawing-per-curve-type)
11. [Type conversion mathematics](#11-type-conversion-mathematics)
12. [Numerical and performance notes](#12-numerical-and-performance-notes)
13. [Quick reference: file / function map](#13-quick-reference-file--function-map)
14. [Appendix A — worked numeric examples](#appendix-a--worked-numeric-examples)
15. [Appendix B — knot vector tables](#appendix-b--knot-vector-tables)

---

## 1. Executive summary

Grease Pencil v3 strokes are *not* a bespoke curve format. A Grease Pencil `Drawing` stores an ordinary
`blender::bke::CurvesGeometry`, exactly the same container used by hair curves and the `Curves` ID.
That means **all four curve types share one evaluation engine**, and Grease Pencil rendering is a thin
consumer on top of it.

The drawing of a stroke splits into two completely separate mathematical stages:

| Stage | What it does | Where |
|---|---|---|
| **A. Curve evaluation** | Turns *control points* into a dense *polyline* of "evaluated points". This is where Catmull-Rom / Bézier / NURBS math lives. | `blenkernel/intern/curve_*.cc`, `curves_geometry.cc` |
| **B. Ribbon expansion** | Turns that polyline into screen-space quads with radius, caps, miters, UVs and per-point attributes. **This stage is curve-type agnostic** — it only ever sees evaluated points. | `draw/intern/draw_cache_impl_grease_pencil.cc`, `draw/intern/shaders/draw_grease_pencil_lib.glsl` |

So the answer to "how does Blender draw a NURBS Grease Pencil stroke?" is:
*it converts it into a polyline using Cox–de Boor basis functions, then draws that polyline the exact
same way it draws a Poly stroke.* Every type differs only in stage A.

A key architectural consequence: the ribbon geometry is built from
`curves.evaluated_positions()` and `curves.evaluated_points_by_curve()`
(`draw_cache_impl_grease_pencil.cc:1377-1378`), and every per-point attribute
(radius, opacity, rotation, vertex colour) is pushed through the *same* interpolation machinery via
`attribute_interpolate()` (`:1166-1176`). Positions and attributes therefore always stay in lock-step,
whatever the curve type.

---

## 2. The data model: where a Grease Pencil stroke actually lives

### 2.1 Curve types

`source/blender/makesdna/DNA_curves_types.h:29-60`:

```c
enum CurveType : int8_t {
  CURVE_TYPE_CATMULL_ROM = 0,
  CURVE_TYPE_POLY        = 1,
  CURVE_TYPE_BEZIER      = 2,
  CURVE_TYPE_NURBS       = 3,
};
#define CURVE_TYPES_NUM 4
```

Note the default value is `CURVE_TYPE_CATMULL_ROM = 0`, which is why
`CurvesGeometry::curve_types()` returns Catmull-Rom when the `curve_type` attribute is absent
(`curves_geometry.cc:229-236`) and why `fill_curve_types()` *deletes* the attribute when filling with
Catmull-Rom (`:244-251`). Grease Pencil, however, creates strokes as **Poly** — see
`rna_grease_pencil_api.cc:64` and `sculpt_paint/grease_pencil/paint.cc:513`. A freshly drawn stroke is a
dense polyline of sampled cursor positions; the other three types only appear after a fit/convert step
(§11).

### 2.2 Handle types (Bézier only)

`DNA_curves_types.h:62-73`:

```c
enum HandleType : int8_t {
  BEZIER_HANDLE_FREE   = 0,  /* free-floating */
  BEZIER_HANDLE_AUTO   = 1,  /* computed for smoothness */
  BEZIER_HANDLE_VECTOR = 2,  /* points at the neighbour control point */
  BEZIER_HANDLE_ALIGN  = 3,  /* collinear with the opposite handle */
};
```

### 2.3 Knot modes (NURBS only)

`DNA_curves_types.h:75-81`:

```c
enum KnotsMode : int8_t {
  NURBS_KNOT_MODE_NORMAL          = 0,
  NURBS_KNOT_MODE_ENDPOINT        = 1,
  NURBS_KNOT_MODE_BEZIER          = 2,
  NURBS_KNOT_MODE_ENDPOINT_BEZIER = 3,
  NURBS_KNOT_MODE_CUSTOM          = 4,
};
```

### 2.4 Attributes that drive evaluation

| Attribute | Domain | Default | Accessor |
|---|---|---|---|
| `curve_type` | Curve | `CATMULL_ROM` (0) | `curves_geometry.cc:229` |
| `cyclic` | Curve | `false` | `:362` |
| `resolution` | Curve | **12** | `:373-382` |
| `nurbs_order` | Curve | **4** (cubic) | `:470-479` |
| `nurbs_knots_mode` | Curve | `NORMAL` (0) | `:495-501` |
| `nurbs_weight` | Point | 1.0 (optional) | `:481-493` |
| `handle_left/right` + `handle_type_left/right` | Point | — | `:406-…` |
| `position`, `radius`, `opacity`, `rotation`, `vertex_color`, `miter_angle` | Point | — | GP-specific ones read in the draw cache |

`remove_attributes_based_on_types()` (`curves_geometry.cc:1765-1782`) drops NURBS/Bézier attributes and
even `resolution` when no curve needs them — a nice hint about which attribute belongs to which type.

### 2.5 Storage layout

All points of all strokes in one drawing live in flat arrays; a curve's slice is obtained through
`points_by_curve()` (an `OffsetIndices<int>`). The evaluated points get their own parallel offset array,
`evaluated_points_by_curve()`. Both are `IndexRange`-sliced everywhere — this is why nearly every
function in `curve_*.cc` takes `Span`/`MutableSpan` rather than pointers.

---

## 3. The evaluation pipeline

### 3.1 Step 1 — how many evaluated points?

`CurvesGeometry::evaluated_points_by_curve()` (`curves_geometry.cc:706-732`) lazily fills a cached
offsets array via `calculate_evaluated_offsets()` (`:649-702`):

```cpp
build_offsets(offsets, [&](const int curve_index) -> int {
  const IndexRange points = points_by_curve[curve_index];
  switch (types[curve_index]) {
    case CURVE_TYPE_CATMULL_ROM:
      return curves::catmull_rom::calculate_evaluated_num(
          points.size(), cyclic[curve_index], resolution[curve_index]);
    case CURVE_TYPE_POLY:
      return points.size();
    case CURVE_TYPE_BEZIER: {
      const IndexRange offsets = curves::per_curve_point_offsets_range(points, curve_index);
      curves::bezier::calculate_evaluated_offsets(handle_types_left.slice(points),
                                                  handle_types_right.slice(points),
                                                  cyclic[curve_index],
                                                  resolution[curve_index],
                                                  all_bezier_offsets.slice(offsets));
      return all_bezier_offsets[offsets.last()];
    }
    case CURVE_TYPE_NURBS:
      /* … */
      return curves::nurbs::calculate_evaluated_num(
          points.size(), order, is_cyclic, resolution[curve_index], knots_mode, custom_knots);
  }
});
```

Note the important fast path at `:709-716`: if *every* curve in the drawing is Poly, the evaluated
offsets array is thrown away entirely and `points_by_curve()` is returned directly. Since Grease Pencil
strokes are Poly by default, this is the common case and costs zero memory.

Bézier is the only type that also produces a **per-segment** offsets array (`all_bezier_offsets`),
because its segments can have different lengths (vector segments collapse to 1 point, §6.4).

Summary of evaluated point counts, for `n` control points, resolution `r`, `S = segments_num(n, cyclic)`
(`BKE_curves.hh:574-578`, `S = n` if cyclic and `n>1`, else `n-1`):

| Type | Evaluated point count |
|---|---|
| Poly | `n` |
| Catmull-Rom | `r·S + 1` if open, `max(r·S, 1)` if cyclic |
| Bézier | `Σ over segments (1 if vector segment else r)` `+ 1` if open |
| NURBS | `r · (#non-zero knot spans) + 1` if open, `r · (#spans)` if cyclic; degenerate → `n` |

### 3.2 Step 2 — positions

`CurvesGeometry::evaluated_positions()` (`curves_geometry.cc:829-916`) is the heart. It groups curves by
type via `curves::foreach_curve_by_type()` (`curves_utils.cc:100-119`) and dispatches four lambdas —
`evaluate_catmull`, `evaluate_poly`, `evaluate_bezier`, `evaluate_nurbs` — each of which parallelises
over its own `IndexMask`. Again there's a Poly short-circuit at `:831-836` that returns
`this->positions()` unchanged with **no allocation at all**.

### 3.3 Step 3 — generic attribute evaluation

The same four-way switch exists for arbitrary attributes, in
`evaluate_generic_data_for_curve()` (`curves_geometry.cc:1010-1039`):

```cpp
switch (eval_data.types[curve_index]) {
  case CURVE_TYPE_CATMULL_ROM:
    curves::catmull_rom::interpolate_to_evaluated(src, cyclic, resolution, dst); break;
  case CURVE_TYPE_POLY:
    dst.copy_from(src); break;
  case CURVE_TYPE_BEZIER:
    curves::bezier::interpolate_to_evaluated(src, all_bezier_offsets.slice(offsets), dst); break;
  case CURVE_TYPE_NURBS:
    curves::nurbs::interpolate_to_evaluated(basis_cache, order, weights, src, dst); break;
}
```

This is what Grease Pencil calls (indirectly) for radius, opacity, rotation and vertex colour. Note the
subtlety: **Bézier interpolates attributes *linearly*, not cubically** (§6.6) — positions use the cubic,
attributes do not.

### 3.4 Step 4 — derived caches

* `evaluated_tangents()` — `curves_geometry.cc:918-976`, always uses the *poly* tangent estimator on the
  evaluated polyline, with a Bézier-specific fix-up of the two endpoints.
* `evaluated_normals()` — `:1041-1129`.
* `ensure_evaluated_lengths()` — `:1183-1210`, prefix sums of segment lengths; Grease Pencil uses these
  for the stroke `u` texture coordinate.
* `ensure_nurbs_basis_cache()` — `:764-827`, the Cox–de Boor weights (§7.5).

All of these are `SharedCache`/`ImplicitSharing`-backed and invalidated through
`tag_topology_changed()` / `tag_positions_changed()`.

---

## 4. Poly curves

**File:** `source/blender/blenkernel/intern/curve_poly.cc`

### 4.1 The (non-)math

A poly curve is the identity map. Evaluated point count = control point count
(`curves_geometry.cc:677-678`); evaluation is `array_utils::copy_group_to_group` (`:862-865`); attribute
evaluation is `dst.copy_from(src)` (`:1021-1023`).

Formally, for control points $P_0 \dots P_{n-1}$ the curve is the piecewise linear map

$$C(u) = (1-t)P_i + t\,P_{i+1},\qquad u = i + t,\ t\in[0,1)$$

but Blender never samples it — the vertices *are* the samples. `resolution` is ignored.

### 4.2 Why this matters for Grease Pencil

This is the default type for painted strokes. Cursor samples become vertices 1:1, so the stroke is
exactly what was drawn, and stage A costs nothing. Everything downstream (§9) is designed around this
"evaluated points are a polyline" assumption.

### 4.3 What `curve_poly.cc` actually contains

Despite the name, this file's real job is the **tangent/normal estimator used by every curve type**
(§8), because those are always computed on the evaluated polyline.

---

## 5. Catmull-Rom curves

**File:** `source/blender/blenkernel/intern/curve_catmull_rom.cc`
**Inline math:** `BKE_curves.hh:806-855`

### 5.1 Mathematical definition

The uniform (centripetal-free, tension $\tau = \tfrac12$) Catmull-Rom spline is the cubic Hermite spline
whose tangent at $P_i$ is the central difference

$$m_i = \frac{P_{i+1} - P_{i-1}}{2}.$$

Substituting into the Hermite basis gives, for the segment $P_1 \to P_2$ influenced by
$(P_0,P_1,P_2,P_3)$ and $t \in [0,1]$:

$$C(t)=\tfrac12\Big[(-t+2t^2-t^3)P_0+(2-5t^2+3t^3)P_1+(t+4t^2-3t^3)P_2+(-t^2+t^3)P_3\Big].$$

The curve **interpolates** its control points ($C(0)=P_1$, $C(1)=P_2$) and is $C^1$ continuous.

### 5.2 Blender's basis, and why it looks different

`curve_catmull_rom.cc:28-39`:

```cpp
float4 calculate_basis(const float parameter)
{
  /* Adapted from Cycles #catmull_rom_basis_eval function. */
  const float t = parameter;
  const float s = 1.0f - parameter;
  return {
      -t * s * s,
      2.0f + t * t * (3.0f * t - 5.0f),
      2.0f + s * s * (3.0f * s - 5.0f),
      -s * t * t,
  };
}
```

This is a **symmetry-exploiting refactor** of the polynomial above. Writing $s = 1-t$:

| Code weight | Expansion | Standard coefficient |
|---|---|---|
| $-t s^2$ | $-t(1-2t+t^2) = -t + 2t^2 - t^3$ | $w_0$ ✔ |
| $2 + t^2(3t-5)$ | $2 - 5t^2 + 3t^3$ | $w_1$ ✔ |
| $2 + s^2(3s-5)$ | $(1-2t+t^2)(-2-3t) + 2 = t + 4t^2 - 3t^3$ | $w_2$ ✔ |
| $-s t^2$ | $-t^2 + t^3$ | $w_3$ ✔ |

so the weight vector is exactly $2\cdot(w_0,w_1,w_2,w_3)$. Note the beautiful structural symmetry:
$w_2(t) = w_1(s)$ and $w_3(t) = w_0(s)$ — the basis is palindromic under $t \leftrightarrow 1-t$,
which is why the code can share the `2.0f + x*x*(3*x-5)` expression.

The missing factor of $\tfrac12$ is applied in the mixer (`BKE_curves.hh:841-853`):

```cpp
template<typename T>
T interpolate(const T &a, const T &b, const T &c, const T &d, const float parameter)
{
  const float4 weights = calculate_basis(parameter);
  if constexpr (is_same_any_v<T, float, float2, float3>) {
    /* Save multiplications by adjusting weights after mix. */
    return 0.5f * attribute_math::mix4<T>(weights, a, b, c, d);
  }
  else {
    return attribute_math::mix4<T>(weights * 0.5f, a, b, c, d);
  }
}
```

For `float3` positions it is cheaper to scale the **result** (3 multiplies) than the **weights**
(4 multiplies) — for generic types (e.g. colours, where `mix4` clamps or converts) the weights must be
pre-scaled to keep the mix normalised.

Sanity check (verified numerically): with $P = \{(0,0),(1,1),(2,0),(3,1)\}$, $t=0.25$ the code yields
$(1.25,\,0.84375)$, identical to the textbook polynomial.

### 5.3 Sampling a segment

`curve_catmull_rom.cc:41-49`:

```cpp
template<typename T>
static void evaluate_segment(const T &a, const T &b, const T &c, const T &d, MutableSpan<T> dst)
{
  const float step = 1.0f / dst.size();
  dst.first() = b;                                  /* exact, avoids FP error at t = 0 */
  for (const int i : dst.index_range().drop_front(1)) {
    dst[i] = interpolate<T>(a, b, c, d, i * step);
  }
}
```

Two details worth noting:

* Each segment writes samples at $t = 0, \tfrac1r, \dots, \tfrac{r-1}{r}$ — the **right endpoint is
  excluded** and belongs to the next segment. This is why an open curve needs `+1` trailing point.
* `dst.first() = b` sets $t=0$ **by assignment rather than by evaluation**. At $t=0$ the basis is
  $(0,2,0,0)\cdot\tfrac12 = (0,1,0,0)$ exactly, but the direct write guarantees bit-exact interpolation
  through the control point regardless of rounding.

### 5.4 Boundary conditions (the interesting part)

The segment $P_1\to P_2$ needs $P_0$ and $P_3$. At the ends of an open curve those don't exist.
`interpolate_to_evaluated()` (`:57-111`) handles this by **duplicating the endpoint**:

```cpp
if (cyclic) {
  evaluate_segment(src.last(),   src[0],      src[1],     src[2],       dst.slice(first));
  evaluate_segment(src.last(2),  src.last(1), src.last(), src.first(),  dst.slice(second_to_last));
  evaluate_segment(src.last(1),  src.last(),  src[0],     src[1],       dst.slice(last));
}
else {
  evaluate_segment(src[0],       src[0],      src[1],     src[2],       dst.slice(first));
  evaluate_segment(src.last(2),  src.last(1), src.last(), src.last(),   dst.slice(second_to_last));
  dst.last() = src.last();
}
```

Mathematically, duplicating $P_0$ sets the phantom point $P_{-1} = P_0$, hence the start tangent becomes

$$m_0 = \frac{P_1 - P_{-1}}{2} = \frac{P_1 - P_0}{2},$$

i.e. **half** the chord — a "natural-ish" end condition that keeps the curve from over-shooting at the
ends. (Contrast with the reflected phantom $P_{-1} = 2P_0 - P_1$, which Bézier auto-handles use — see
§6.3. Blender deliberately uses different conventions in the two places.)

Cyclic curves wrap around, so the last, first and second-to-last segments each need special index
juggling before the bulk parallel loop over the interior:

```cpp
const IndexRange inner_range = src.index_range().drop_back(2).drop_front(1);
threading::parallel_for(inner_range, 512, [&](IndexRange range) {
  for (const int i : range) {
    evaluate_segment(src[i - 1], src[i], src[i + 1], src[i + 2], dst.slice(range_fn(i)));
  }
});
```

Degenerate cases:

* **1 point** → `dst.first() = src.first()` (`:68-71`).
* **2 points** → `evaluate_segment(P0, P0, P1, P1, …)`. With both phantoms clamped, the basis collapses:
  $w_0+w_1 = 2 - t + 2t^2 - t^3 - 5t^2 + 3t^3 \dots$ — in effect the Catmull-Rom reduces to a smooth
  ease between the two points (not an exact straight line, but monotone and endpoint-interpolating).

### 5.5 Evaluated point count

`curve_catmull_rom.cc:16-26`:

```cpp
int calculate_evaluated_num(const int points_num, const bool cyclic, const int resolution)
{
  const int points_per_segment = std::max(1, resolution);
  const int eval_num = points_per_segment * segments_num(points_num, cyclic);
  if (cyclic) {
    return std::max(eval_num, 1);   /* single-point cyclic curve */
  }
  return eval_num + 1;              /* trailing endpoint */
}
```

With the default `resolution = 12`, a 10-point open Catmull-Rom stroke evaluates to
$12 \times 9 + 1 = 109$ points, i.e. **109 quads** in the Grease Pencil ribbon.

### 5.6 Two dispatch flavours

There are two `interpolate_to_evaluated` overloads: one taking a uniform `resolution`
(`:114-130`) and one taking an `OffsetIndices<int> evaluated_offsets` (`:132-144`). They share the
implementation through a `RangeForSegmentFn` template parameter — a compile-time strategy that avoids
materialising an offsets array in the common uniform-resolution case:

```cpp
[resolution](const int segment_i) -> IndexRange {
  const int points_per_segment = std::max(1, resolution);
  return {segment_i * points_per_segment, points_per_segment};
}
```

### 5.7 Grease-Pencil-specific behaviour

* Catmull-Rom strokes are *smooth by construction*, so the Grease Pencil corner/miter logic explicitly
  skips them: `interpolate_corners()` in `draw_cache_impl_grease_pencil.cc:1220-1224` leaves all
  evaluated miter angles at `GP_STROKE_MITER_ANGLE_ROUND` with the comment
  *"NUBRS and Catmull-Rom are continuous and don't have corners."*
* Radius/opacity/colour are interpolated with the **same cubic basis** (via
  `attribute_interpolate()` → `interpolate_to_evaluated`), so a Catmull-Rom stroke's thickness profile is
  also a Catmull-Rom spline, not a linear ramp. For `int`/`bool`/`int8_t` attributes `mix4` rounds or
  thresholds (`BKE_attribute_math.hh:219-238`), and note that the Catmull-Rom basis has **negative
  lobes** ($w_0, w_3 \le 0$), so a cubic interpolation of e.g. radius can overshoot below the minimum
  control radius — the draw code clamps with `math::max(radii[point_i], 0.0f)`
  (`draw_cache_impl_grease_pencil.cc:1443`).

---

## 6. Bézier curves

**File:** `source/blender/blenkernel/intern/curve_bezier.cc`
**Inline helpers:** `BKE_curves.hh:639-801`, `:1113-1146`

### 6.1 Representation

Each control point stores three positions: `position`, `handle_left`, `handle_right`, plus
`handle_type_left` / `handle_type_right`. Segment $i$ is the cubic Bézier

$$B_i(t) = (1-t)^3 P_i + 3(1-t)^2 t\,R_i + 3(1-t)t^2 L_{i+1} + t^3 P_{i+1}$$

with $R_i$ = right handle of point $i$, $L_{i+1}$ = left handle of point $i+1$. Cyclic curves add the
wrap segment $(P_{n-1}, R_{n-1}, L_0, P_0)$.

### 6.2 Handle solving — `calculate_point_handles`

`curve_bezier.cc:146-213`. Called from `calculate_auto_handles()` (`:263-303`), which handles the two
end points specially and parallelises the interior.

**AUTO handles.** Given the neighbour offsets $d_p = P_i - P_{i-1}$, $d_n = P_{i+1} - P_i$, define the
normalised bisector

$$\mathbf{d} = \frac{d_n}{\|d_n\|} + \frac{d_p}{\|d_p\|},\qquad \ell = 2.5614\,\|\mathbf{d}\|$$

then

$$L_i = P_i - \mathbf{d}\,\frac{\min(\|d_p\|,\,5\|d_n\|)}{\ell},\qquad
  R_i = P_i + \mathbf{d}\,\frac{\min(\|d_n\|,\,5\|d_p\|)}{\ell}.$$

The magic constant is documented in-source (`:161-176`):

> The magic number 2.5614 is derived from approximating a circular arc at the control point. Given the
> constraints `P0=(0,1),P1=(c,1),P2=(1,c),P3=(1,0)`; the first derivative of the curve must agree with
> the circular arc derivative at the endpoints; minimize the maximum radial drift — one can compute
> `c ≈ 0.5519150244935105707435627`. The distance from P0 to P3 is `sqrt(2)`. The magic factor for `len`
> is `(sqrt(2) / 0.5519…) ≈ 2.562375546255352`. In older code of blender a slightly worse approximation
> of 2.5614 is used. It's kept for compatibility.

In other words: for a quarter-circle, this handle length makes the cubic Bézier an optimal circular
approximation. The $\min(\cdot, 5\times)$ clamp prevents a very long segment from producing a handle
that overshoots into a very short neighbouring segment (the classic "auto handle loop" artifact).

**VECTOR handles.** `BKE_curves.hh:1139-1142`:

```cpp
inline float3 calculate_vector_handle(const float3 &point, const float3 &next_point)
{
  return math::interpolate(point, next_point, 1.0f / 3.0f);
}
```

i.e. $R_i = P_i + \tfrac13(P_{i+1}-P_i)$. Both handles of a segment at $\tfrac13$ and $\tfrac23$ make the
cubic Bézier **exactly the straight line** — substituting into the Bernstein form gives
$B(t) = P_i + t(P_{i+1}-P_i)$. That algebraic identity is what licenses the optimisation in §6.4.

**ALIGN handles.** `calculate_aligned_handle()` (`:89-100`) mirrors direction while preserving length:

$$L_i = P_i - \|L_i - P_i\| \cdot \frac{R_i - P_i}{\|R_i - P_i\|}$$

guaranteeing $C^1$ (collinear tangents, arbitrary magnitudes). `calculate_align_both_handles()`
(`:102-144`) handles the harder "both handles are ALIGN" case: it takes the normalised bisector
$\hat a$ of the two handle directions, projects each handle direction onto the plane orthogonal to
$\hat a$… wait, precisely:

```cpp
const float3 new_left_dir  = left_dir  - math::dot(left_dir,  align_dir) * align_dir;
const float3 new_right_dir = right_dir - math::dot(right_dir, align_dir) * align_dir;
return {position + left_length  * math::normalize(new_left_dir),
        position + right_length * math::normalize(new_right_dir)};
```

This removes the component *along* the bisector, leaving directions that are symmetric about it, then
restores the original lengths — so both handles end up anti-parallel, in the plane the user drew them
in, with lengths preserved.

**FREE handles** are untouched.

### 6.3 End-point phantom convention

`calculate_auto_handles()` (`:275-283`, `:295-302`) feeds phantom neighbours to the first/last point:

```cpp
calculate_point_handles(…, positions.first(),
                        cyclic ? positions.last() : 2.0f * positions.first() - positions[1],
                        positions[1], …);
```

For the open case, $P_{-1} = 2P_0 - P_1$ — the **reflection** of $P_1$ through $P_0$. That makes the
bisector at the endpoint parallel to the first chord, so an AUTO end handle points straight down the
first segment. (Compare with Catmull-Rom's clamped phantom, §5.4 — different end conditions for
different reasons.)

### 6.4 Variable segment resolution — the vector-segment optimisation

`curve_bezier.cc:33-66`:

```cpp
void calculate_evaluated_offsets(const Span<int8_t> handle_types_left,
                                 const Span<int8_t> handle_types_right,
                                 const bool cyclic, const int resolution,
                                 MutableSpan<int> evaluated_offsets)
{
  const int points_per_segment = std::max(1, resolution);
  int offset = 0;
  for (const int i : IndexRange(size - 1)) {
    evaluated_offsets[i] = offset;
    offset += segment_is_vector(handle_types_left, handle_types_right, i) ? 1 : points_per_segment;
  }
  evaluated_offsets.last(1) = offset;
  if (cyclic) {
    offset += last_cyclic_segment_is_vector(handle_types_left, handle_types_right) ? 1
                                                                                   : points_per_segment;
  }
  else { offset++; }
  evaluated_offsets.last() = offset;
}
```

with (`BKE_curves.hh:1122-1130`)

```cpp
inline bool segment_is_vector(const HandleType left, const HandleType right)
{ return left == BEZIER_HANDLE_VECTOR && right == BEZIER_HANDLE_VECTOR; }
```

If both handles bounding a segment are VECTOR, the segment is provably a straight line (§6.2), so it is
allocated **a single evaluated point** instead of `resolution` of them. For Grease Pencil this matters a
lot: a Bézier stroke with many straight runs costs far fewer ribbon quads.

This is also *why* Bézier needs the extra `all_bezier_offsets` array (`curves_geometry.cc:719-727`,
sized `points_num + curves_num`) — every other type can compute a segment's destination range
arithmetically.

`has_vector_handles()` (`BKE_curves.hh:1132-1137`) lets callers cheaply detect whether any collapse
happened, by comparing the actual evaluated count against `segments_num · resolution`.

### 6.5 Evaluation by forward differencing

`curve_bezier.cc:305-327` — this is the mathematically most interesting routine in the file:

```cpp
template<typename T>
void evaluate_segment_ex(const T &point_0, const T &point_1, const T &point_2, const T &point_3,
                         MutableSpan<T> result)
{
  const float inv_len         = 1.0f / float(result.size());
  const float inv_len_squared = inv_len * inv_len;
  const float inv_len_cubed   = inv_len_squared * inv_len;

  const T rt1 = 3.0f * (point_1 - point_0) * inv_len;
  const T rt2 = 3.0f * (point_0 - 2.0f * point_1 + point_2) * inv_len_squared;
  const T rt3 = (point_3 - point_0 + 3.0f * (point_1 - point_2)) * inv_len_cubed;

  T q0 = point_0;
  T q1 = rt1 + rt2 + rt3;
  T q2 = 2.0f * rt2 + 6.0f * rt3;
  T q3 = 6.0f * rt3;
  for (const int i : result.index_range()) {
    result[i] = q0;
    q0 += q1;  q1 += q2;  q2 += q3;
  }
}
```

**Derivation.** Write the cubic in monomial form,
$B(t) = a_0 + a_1 t + a_2 t^2 + a_3 t^3$, where from the Bernstein form

$$a_0 = P_0,\quad a_1 = 3(P_1 - P_0),\quad a_2 = 3(P_0 - 2P_1 + P_2),\quad a_3 = P_3 - P_0 + 3(P_1 - P_2).$$

Those are precisely `rt1`, `rt2`, `rt3` **pre-scaled by $h, h^2, h^3$** where $h = 1/n$ is the parameter
step. Define $f(i) = B(ih)$. The forward differences are

$$\begin{aligned}
\Delta f(i)   &= f(i{+}1) - f(i)         &&= a_1h + a_2h^2 + a_3h^3 + \dots \\
\Delta^2 f(i) &= \Delta f(i{+}1)-\Delta f(i) &&= 2a_2h^2 + 6a_3h^3 + \dots \\
\Delta^3 f    &= 6a_3h^3 &&= \text{constant.}
\end{aligned}$$

Evaluating at $i=0$ gives exactly the initialisers in the code:
$q_0 = a_0$, $q_1 = a_1h + a_2h^2 + a_3h^3$, $q_2 = 2a_2h^2 + 6a_3h^3$, $q_3 = 6a_3h^3$.
The loop is then the standard Pascal-triangle update, producing each sample in **3 vector additions**
and zero multiplications — versus 11 multiply-adds for direct Bernstein evaluation. I verified this
numerically: forward differencing and direct Bernstein evaluation agree to the last bit for an 8-sample
segment.

The trade-off is error accumulation: FP error compounds over $n$ steps as roughly $O(n)$ (per-step
rounding, not $O(n^2)$, since only the highest difference is constant). At typical resolutions
($r \le 64$) this is far below pixel accuracy. The routine is explicitly instantiated only for `float3`
and `float2` (`:328-345`) — the `float2` variant is used by the Grease Pencil paint tool when
sampling fitted curves in screen space (`sculpt_paint/grease_pencil/paint.cc:96-119`).

### 6.6 Assembling the whole curve

`calculate_evaluated_positions()` (`curve_bezier.cc:347-398`):

* segment 0 evaluated first (needed to seed cache-friendly ordering);
* interior segments in `threading::parallel_for` with an adaptive grain size
  `max(evaluated_size / points_size * 32, 1)` — *"Give each task fewer segments as the resolution gets
  larger"* (`:365`);
* per segment, `if (evaluated_range.size() == 1) evaluated_positions[…] = positions[i];` — the
  vector-segment fast path;
* the cyclic wrap segment `(P_last, R_last, L_first, P_first)` last (`:387-397`).

### 6.7 Attribute interpolation is *linear*

`curve_bezier.cc:400-434`:

```cpp
template<typename T>
static inline void linear_interpolation(const T &a, const T &b, MutableSpan<T> dst)
{
  dst.first() = a;
  const float step = 1.0f / dst.size();
  for (const int i : dst.index_range().drop_front(1)) {
    dst[i] = attribute_math::mix2(i * step, a, b);
  }
}
```

Positions follow the cubic; **radius, opacity, colour, rotation follow a straight ramp in the segment
parameter $t$**. This is a deliberate design decision (handles only exist for position — see the
`CURVE_TYPE_BEZIER` comment in `DNA_curves_types.h:46-51`: *"Handles are stored separately from
positions, and do not store extra generic attribute values."*).

A visible consequence for Grease Pencil: on a Bézier stroke, thickness varies **linearly in the curve
parameter**, not in arc length. Since the cubic's speed $\|B'(t)\|$ is not constant, thickness gradients
appear compressed where the curve moves fast.

### 6.8 De Casteljau subdivision (used by editing, not drawing)

`curve_bezier.cc:69-87`:

```cpp
Insertion insert(const float3 &point_prev, const float3 &handle_prev,
                 const float3 &handle_next, const float3 &point_next, float parameter)
{
  /* De Casteljau Bezier subdivision. */
  const float3 center_point = math::interpolate(handle_prev, handle_next, parameter);
  Insertion result;
  result.handle_prev  = math::interpolate(point_prev,          handle_prev,        parameter);
  result.handle_next  = math::interpolate(handle_next,         point_next,         parameter);
  result.left_handle  = math::interpolate(result.handle_prev,  center_point,       parameter);
  result.right_handle = math::interpolate(center_point,        result.handle_next, parameter);
  result.position     = math::interpolate(result.left_handle,  result.right_handle,parameter);
  return result;
}
```

The textbook three-level lerp pyramid; it splits one cubic into two cubics that trace the identical
curve. Used by subdivide/insert-point operators.

### 6.9 Bézier and Grease Pencil corners

Bézier is the only type that can have genuine geometric corners (VECTOR/FREE handles), and
`draw_cache_impl_grease_pencil.cc:1213-1219` reflects that:

```cpp
case CURVE_TYPE_BEZIER: {
  const Span<int> offsets = curves.bezier_evaluated_offsets_for_curve(curve_i);
  for (const int i : points.index_range()) {
    eval_corners_range[offsets[i]] = miter_angles[points[i]];
  }
  break;
}
```

The user-authored `miter_angle` is written **only at the evaluated indices that correspond to control
points**; every interpolated point in between keeps the default "round" value. That's exactly right: a
miter/bevel join is only meaningful where the tangent can be discontinuous.

`bezier_evaluated_offsets_for_curve()` (`BKE_curves.hh:1070-1077`) is the accessor into the shared
`all_bezier_offsets` array.

---

## 7. NURBS curves

**File:** `source/blender/blenkernel/intern/curve_nurbs.cc`
**Cache struct:** `BKE_curves.hh:50-68`
**Inline size helpers:** `BKE_curves.hh:1154-1167`

### 7.1 Definition

A NURBS curve of order $k$ (degree $p = k-1$) with control points $P_i$, weights $w_i$ and knot vector
$U = \{u_0 \le u_1 \le \dots\}$ is

$$C(u) = \frac{\sum_{i} N_{i,p}(u)\,w_i\,P_i}{\sum_{i} N_{i,p}(u)\,w_i}$$

with the Cox–de Boor recursion

$$N_{i,0}(u) = \begin{cases}1 & u_i \le u < u_{i+1}\\ 0 & \text{else}\end{cases},\qquad
N_{i,p}(u) = \frac{u-u_i}{u_{i+p}-u_i}N_{i,p-1}(u) + \frac{u_{i+p+1}-u}{u_{i+p+1}-u_{i+1}}N_{i+1,p-1}(u).$$

Only $p+1 = k$ basis functions are non-zero on any span, which is the whole basis of Blender's caching
strategy.

### 7.2 Sizes

`BKE_curves.hh:1156-1165`:

```cpp
inline int knots_num(const int points_num, const int8_t order, const bool cyclic)
{
  /* Cyclic: points_num + order * 2 - 1 */
  return points_num + order + cyclic * (order - 1);
}

inline int control_points_num(const int points_num, const int8_t order, const bool cyclic)
{
  return points_num + cyclic * (order - 1);
}
```

This is the textbook count: $N$ control points of degree $p$ need $N + p + 1 = N + k$ knots. Cyclic
curves append $k-1$ extra wrap knots, which is exactly the count required by the $k-1$ *virtual*
repeated control points (`control_points_num`) — those are never stored, only synthesised at runtime by
the modulo in §7.7. Consistency check: a cyclic curve has $N+k-1$ effective control points, so it needs
$(N+k-1)+k = N + 2k - 1$ knots, matching `points_num + order + (order - 1)`. ✔

The basis code only ever indexes up to `span_index + degree` (asserted at `curve_nurbs.cc:209`), so the
final knot is read but never differenced.

### 7.3 Validity gate

`curve_nurbs.cc:16-41`:

```cpp
bool check_valid_eval_params(const int points_num, const int8_t order, const bool cyclic,
                             const KnotsMode knots_mode, const int resolution)
{
  if (points_num < order) return false;
  if (order < 2)          return false;
  if (resolution < 1)     return false;
  if (ELEM(knots_mode, NURBS_KNOT_MODE_BEZIER, NURBS_KNOT_MODE_ENDPOINT_BEZIER)) {
    if (knots_mode == NURBS_KNOT_MODE_BEZIER && points_num <= order) return false;
    return (!cyclic || points_num % (order - 1) == 0);
  }
  return true;
}
```

If this fails, the curve **degenerates to its control polygon**: `calculate_evaluated_num` returns
`points_num` (`:95-97`), the basis cache is flagged `invalid` (`curves_geometry.cc:799-803`), and
`interpolate_to_evaluated` short-circuits with `dst.copy_from(src)` (`curve_nurbs.cc:363-366`). So an
under-specified NURBS Grease Pencil stroke silently renders as a poly line rather than disappearing.
The unit tests `NURBSEvaluateZeroOrderBezierDeg3` / `…ClampedDeg3`
(`curves_geometry_test.cc:544-580`) assert exactly this for `order ∈ {-1, 0, 1}`.

The Bezier-mode divisibility condition `points_num % (order - 1) == 0` is because in Bezier knot mode
every interior knot has multiplicity $p$, so the control points must partition cleanly into
Bézier-like blocks of $p$.

### 7.4 Knot vector generation

`curve_nurbs.cc:121-163` — one loop that covers all four built-in modes:

```cpp
const bool is_bezier    = ELEM(mode, NURBS_KNOT_MODE_BEZIER, NURBS_KNOT_MODE_ENDPOINT_BEZIER);
const bool is_end_point = ELEM(mode, NURBS_KNOT_MODE_ENDPOINT, NURBS_KNOT_MODE_ENDPOINT_BEZIER);
/* Inner knots are always repeated once except on Bezier case. */
const int repeat_inner = is_bezier ? order - 1 : 1;
/* How many times to repeat 0.0 at the beginning of knot. */
const int head = is_end_point ? (order - (cyclic ? 1 : 0))
                              : (is_bezier ? min_ii(2, repeat_inner) : 1);
/* Number of knots replicating widths of the starting knots. */
const int tail = cyclic ? 2 * order - 1 : (is_end_point ? order : 0);

int r = head;  float current = 0.0f;
const int offset = is_end_point && cyclic ? 1 : 0;
if (offset) { knots[0] = current; current += 1.0f; }

for (const int i : IndexRange(offset, knots.size() - offset - tail)) {
  knots[i] = current;
  r--;
  if (r == 0) { current += 1.0; r = repeat_inner; }
}

const int tail_index = knots.size() - tail;
for (const int i : IndexRange(tail)) {
  knots[tail_index + i] = current + (knots[i] - knots[0]);
}
```

The state machine is: emit `current` `head` times, then bump; thereafter emit each value
`repeat_inner` times. The tail replicates the head's *spacing pattern* shifted to the end — which is
what makes a cyclic curve's basis periodic and an endpoint curve's basis clamped.

The resulting vectors (verified by re-implementing this loop and running it — see Appendix B) for
`points=6, order=4`:

| Mode | Cyclic | Knots |
|---|---|---|
| NORMAL | no | `0 1 2 3 4 5 6 7 8 9` |
| NORMAL | yes | `0 1 2 3 4 5 6 7 8 9 10 11 12` |
| ENDPOINT | no | `0 0 0 0 1 2 3 3 3 3` |
| ENDPOINT | yes | `0 1 1 1 2 3 4 5 5 5 6 7 8` |
| BEZIER | no | `0 0 1 1 1 2 2 2 3 3` |
| ENDPOINT_BEZIER | no | `0 0 0 0 1 1 1 1 1 1` |

* **NORMAL** = uniform, unclamped → the curve does *not* touch its first/last control points and only
  spans the interior; this is why a NORMAL open NURBS looks "pulled in" at the ends.
* **ENDPOINT** = clamped (multiplicity $k$ at both ends) → interpolates the end control points.
* **BEZIER** / **ENDPOINT_BEZIER** = interior multiplicity $p$ → the curve becomes a chain of
  independent Bézier segments ($C^0$ at the joins).
* **CUSTOM** takes user knots via `load_curve_knots()` (`:165-181`) / `copy_custom_knots()`
  (`:105-119`), which for cyclic curves synthesises the wrap tail as
  `tail[i] = knots[order + i] + (last_knot - knots[order-1])`.

### 7.5 How many evaluated points? — breakpoint counting

This is the subtlest part of the NURBS implementation, because with repeated knots many spans have
**zero length** and must not be sampled.

`is_breakpoint()` (`:67-70`):

```cpp
static bool is_breakpoint(const Span<float> knots, const int knot_span)
{ return (knots[knot_span + 1] - knots[knot_span]) > 0.0f; }
```

Two counters exist. For built-in modes the count is derived analytically (`:44-65`):

```cpp
static int calc_nonzero_knot_spans(const int points_num, const KnotsMode mode,
                                   const int8_t order, const bool cyclic)
{
  const int repeat_inner = is_bezier ? order - 1 : 1;
  const int knots_before_geometry = order + int(is_bezier && !is_end_point && order > 2);
  const int knots_after_geometry  = order - 1 +
                                    (cyclic && mode == NURBS_KNOT_MODE_ENDPOINT ? order - 2 : 0);
  const int knots_total    = knots_num(points_num, order, cyclic);
  const int geometry_knots = knots_total - knots_before_geometry - knots_after_geometry;
  const int non_zero_knots = (geometry_knots + repeat_inner - 1) / repeat_inner;  /* ceil */
  return non_zero_knots;
}
```

For `NURBS_KNOT_MODE_CUSTOM` with actual knots present, it instead literally counts breakpoints
(`:72-86`):

```cpp
const int wrapped_points_num = control_points_num(points_num, order, cyclic);
for (const int knot_span : IndexRange::from_begin_end(degree, wrapped_points_num)) {
  span_num += is_breakpoint(knots, knot_span);
}
```

Then (`:88-103`):

```cpp
return resolution * nonzero_span_num + int(!cyclic);
```

Same pattern as Catmull-Rom: `resolution` samples per span, plus a terminating point for open curves.
Concrete counts at `resolution = 12`, `points = 6`, `order = 4`: NORMAL open → 37; NORMAL cyclic → 72;
ENDPOINT cyclic → 48; BEZIER open → 13.

### 7.6 The basis cache

`BKE_curves.hh:50-68`:

```cpp
struct BasisCache {
  /** For each evaluated point, the weight for all control points that influences it.
   *  The vector's size is the evaluated point count multiplied by the curve's order. */
  Vector<float> weights;
  /** For each evaluated point, an offset into the curve's control points for the start of #weights. */
  Vector<int> start_indices;
  bool invalid = false;
};
```

Because only $k$ basis functions are non-zero at any $u$, each evaluated point needs exactly
`order` floats plus one start index. Total: `evaluated_num * order` floats. This cache is
**position-independent** — it depends only on knots/order/resolution — so moving control points does not
invalidate it. That's the whole point: dragging a NURBS Grease Pencil point re-runs a weighted sum, not
a Cox–de Boor recursion.

The per-point recursion is the classical Algorithm A2.2 from *The NURBS Book*, cited in-source
(`:203-204`, `:205-235`):

```cpp
/* Basis function calculation, implementation based on 'The NURBS Book' p. 70, ISBN: 3540615458. */
static void calculate_basis_for_point(const Span<float> knots, const int degree,
                                      const float parameter, const int span_index,
                                      MutableSpan<float> r_weights, int &r_start_index)
{
  const int order = degree + 1;
  r_start_index = span_index - degree;

  Array<float, 12> left(order);
  Array<float, 12> right(order);
  r_weights[0] = 1.0f;

  for (const int j : IndexRange(1, degree)) {
    left[j]  = parameter - knots[span_index + 1 - j];
    right[j] = knots[span_index + j] - parameter;
    float saved = 0.0f;
    for (const int r : IndexRange(j)) {
      const float temp = r_weights[r] / (right[r + 1] + left[j - r]);
      r_weights[r] = saved + right[r + 1] * temp;
      saved        = left[j - r] * temp;
    }
    r_weights[j] = saved;
  }
}
```

Why this is numerically nice: the denominators `right[r+1] + left[j-r]` are always **positive knot
spans** (never the `0/0` of the naive recursion), because the algorithm only ever evaluates inside a
non-degenerate span. It is $O(p^2)$, allocation-free for $p < 12$ thanks to `Array<float, 12>`'s inline
buffer, and produces a **partition of unity** ($\sum_j N = 1$) by construction.

`calculate_basis_cache()` (`:237-313`) drives it:

```cpp
const int breakpoint_num = (evaluated_num - !cyclic) / resolution;
Array<int, 20> span_offsets(breakpoint_num);

int breakpoint_count = 0;
for (const int span_index : IndexRange::from_begin_end(degree, wrapped_points_num)) {
  if (is_breakpoint(knots, span_index)) { span_offsets[breakpoint_count++] = span_index; }
}
BLI_assert(breakpoint_count == breakpoint_num);

threading::parallel_for(span_offsets.index_range(), 4096, [&](const IndexRange range) {
  for (const int index : range) {
    const int span_index = span_offsets[index];
    int eval_point = index * resolution;
    const float knot_delta = knots[span_index + 1] - knots[span_index];
    const float knot_step  = knot_delta / resolution;
    for (const int step : IndexRange::from_begin_size(0, resolution)) {
      const float parameter = knots[span_index] + step * knot_step;
      calculate_basis_for_point(knots, degree, parameter, span_index,
                                basis_weights.slice(eval_point * order, order),
                                basis_start_indices[eval_point]);
      eval_point++;
    }
  }
});
if (!cyclic) {
  calculate_basis_for_point(knots, degree, knots[wrapped_points_num], span_offsets.last(),
                            basis_weights.slice(basis_weights.size() - order, order),
                            basis_start_indices.last());
}
```

Key properties:

* Sampling is **uniform in each span's own parameter range**, not globally uniform in $u$. Two spans of
  different knot width both get `resolution` samples, so a non-uniform knot vector produces a
  non-uniform arc-length distribution.
* The half-open convention `[u_i, u_{i+1})` means the right end of each span belongs to the next one;
  the final point of an open curve is handled by the explicit tail call at `knots[wrapped_points_num]`.
* Spans are the parallelism unit (grain 4096).

The unit tests validate this against hand-written quadratic basis polynomials:
`BasisCacheBezierSegmentDeg2` (`curves_geometry_test.cc:589-624`) checks
$N = \{(1-u)^2,\,2u(1-u),\,u^2\}$ for a clamped 3-point order-3 curve, and `BasisCacheNonUniformDeg2`
(`:626-702`) checks five different piecewise formulas across the knot vector
`0 0 0 1 2 3 4 4 5 5 5`.

### 7.7 Applying the basis

Non-rational (`curve_nurbs.cc:316-334`):

```cpp
attribute_math::DefaultMixer<T> mixer{dst};
threading::parallel_for(dst.index_range(), 128, [&](const IndexRange range) {
  for (const int i : range) {
    Span<float> point_weights = basis_cache.weights.as_span().slice(i * order, order);
    for (const int j : point_weights.index_range()) {
      const int point_index = (basis_cache.start_indices[i] + j) % src.size();
      mixer.mix_in(i, src[point_index], point_weights[j]);
    }
  }
  mixer.finalize(range);
});
```

Rational (`:336-356`) differs by one line:

```cpp
const float weight = point_weights[j] * control_weights[point_index];
mixer.mix_in(i, src[point_index], weight);
```

**Where does the division by $\sum N_i w_i$ happen?** Inside the mixer. `SimpleMixer::mix_in` accumulates
`buffer += value * weight` and `total_weights += weight`; `finalize()` does `buffer *= 1/weight`
(`BKE_attribute_math.hh:565-600`). So the rational path computes exactly

$$C(u) = \frac{\sum_j N_j w_j P_j}{\sum_j N_j w_j}$$

and the non-rational path computes $\sum_j N_j P_j / \sum_j N_j$, which is identical to $\sum_j N_j P_j$
because the basis is a partition of unity. Elegant: one code path, two formulas, and the normalisation
falls out of the generic attribute mixing infrastructure.

The `% src.size()` is the **cyclic wrap**: control point indices beyond the end fold back to the
beginning, realising the $k-1$ virtual repeated control points without duplicating any data.

### 7.8 Ground-truth test

`curves_geometry_test.cc:456-543` (`NURBSEvaluation`) pins the exact output for a 4-point, order-4,
resolution-10 curve. Open: 11 evaluated points from `(0.166667, 0.833333, 0)` to
`(-0.166667, 0.166667, 0)` — note it starts *inside* the control polygon, the NORMAL-knot behaviour from
§7.4. Cyclic: 40 points. Weighted: setting `nurbs_weight[0] = 4.0` visibly pulls the curve toward $P_0$,
and the expected values are pinned to 1e-5.

### 7.9 NURBS in Grease Pencil specifically

* NURBS strokes **do not interpolate their control points** (except in ENDPOINT modes), so the drawn
  ribbon can be far from where the user clicked. The edit overlay therefore draws the **control polygon
  as a separate line batch** — `grease_pencil_cache_add_nurbs()`
  (`draw_cache_impl_grease_pencil.cc:419-455`) and `index_buf_add_nurbs_lines()` (`:457-485`) — while the
  evaluated polyline for NURBS curves is *excluded* from the normal edit-line batch via
  `grease_pencil_get_editable_non_nurbs_curves()` (`:282-299`).
* Like Catmull-Rom, NURBS is treated as corner-free by the miter logic (`:1220-1224`).

---

## 8. Tangents and normals (shared by all types)

**File:** `source/blender/blenkernel/intern/curve_poly.cc`

`CurvesGeometry::evaluated_tangents()` (`curves_geometry.cc:918-976`) always calls
`curves::poly::calculate_tangents()` on the **evaluated** polyline — the analytic derivative of the
underlying spline is never used. Only Bézier gets a correction, and only at the two endpoints of open
curves (`:943-975`):

```cpp
if (!math::almost_equal_relative(handles_right[points.first()], positions[points.first()], epsilon)) {
  tangents[evaluated_points.first()] = math::normalize(handles_right[points.first()] -
                                                       positions[points.first()]);
}
```

which snaps the start/end tangent to the handle direction so a Bézier stroke's cap orientation matches
the user's handle.

### 8.1 The bisector estimator

`direction_bisect()` (`curve_poly.cc:33-71`):

$$T_i = \frac{\hat d_{i-1} + \hat d_i}{\|\hat d_{i-1} + \hat d_i\|},\qquad
\hat d_i = \frac{P_{i+1}-P_i}{\|P_{i+1}-P_i\|}$$

The angle-bisector of the two adjacent unit chords. But there's a well-engineered numerical guard:

```cpp
const float3 tangent = prev_dir + other_dir;
const float norm = math::length(tangent);
if (norm < 0.6627619f) { /* Approximates angle between segments < 45 degrees. */
  if (norm < 2e-7) {     /* Approximately < sin(1e-5) */
    return other_dir;
  }
  /* Compute using the cross product, as catastrophic cancellation occurs in `tangent`
   * when the sum approaches 0, leading to significant numerical errors (see #146332). */
  const float3 binormal = math::cross(other_dir, prev_dir);
  const float3 normal   = other_dir - prev_dir;
  return math::normalize(math::cross(binormal, normal));
}
return tangent / norm;
```

When two unit vectors nearly cancel (a hairpin turn), their **sum** loses almost all significant digits —
classic catastrophic cancellation. Their **difference** does not. So the code reconstructs the bisector
as $\widehat{(\hat d_{i-1} \times \hat d_i) \times (\hat d_i - \hat d_{i-1})}$, which is mathematically
the same direction but computed from well-conditioned quantities.

Interpreting the threshold: for two unit vectors separated by angle $\theta$,
$\|\hat a + \hat b\| = 2\cos(\theta/2)$. Setting that to `0.6627619` gives
$\theta = 2\arccos(0.33138) \approx 141.3°$ between the incoming and outgoing chord directions, i.e. an
included corner angle of about $180° - 141.3° \approx 38.7°$. The source comment rounds this to
*"angle between segments < 45 degrees"* (an exact 45° corner would give
$2\cos(67.5°) \approx 0.7654$), so the constant is a slightly tighter, conservative version of the
documented criterion. The inner guard `norm < 2e-7` (≈ $\sin(10^{-5})$) bails out entirely to the
outgoing direction when the two chords are numerically antiparallel and the cross product itself becomes
meaningless.

`calculate_tangents()` (`:74-123`) additionally: finds the first non-degenerate segment and back-fills
the tangents before it, falls back to $(0,0,1)$ for a fully degenerate curve, and closes the loop for
cyclic curves.

### 8.2 Normals

* `calculate_normals_z_up()` (`:126-140`): $N = \widehat{(T_y, -T_x, 0)}$, with the $(1,0,0)$ fallback
  when the tangent is nearly vertical.
* `calculate_normals_minimum()` (`:166-205`): rotation-minimising frame (double-reflection style). Each
  normal is the previous one rotated by the tangent-to-tangent rotation
  (`calculate_next_normal`, `:146-164`), then for cyclic curves the accumulated closure error is measured
  and **distributed linearly** over all points:

```cpp
const float angle_step = correction_angle / normals.size();
for (const int i : normals.index_range()) {
  normals[i] = math::rotate_direction_around_axis(normals[i], tangents[i], angle_step * i);
}
```

For Grease Pencil, curve normals are largely bypassed — the ribbon is screen-facing (§9.3) — but the
per-*stroke* plane normal used for fill triangulation is computed separately with **Newell's method**
(`blenkernel/intern/grease_pencil.cc:679-717`), including a degenerate-collinear fallback.

---

## 9. From evaluated points to pixels: the Grease Pencil draw path

**File:** `source/blender/draw/intern/draw_cache_impl_grease_pencil.cc`
**Shader:** `source/blender/draw/intern/shaders/draw_grease_pencil_lib.glsl`,
`source/blender/draw/engines/gpencil/shaders/gpencil_vert.glsl`

From here on, **curve type is invisible**. Everything reads evaluated data.

### 9.1 Vertex counting

`grease_pencil_geom_batch_ensure()` (`:1280-…`). First pass counts (`:1326-1345`):

```cpp
const OffsetIndices<int> points_by_curve = curves.evaluated_points_by_curve();
…
verts_start_offsets[curve_i] = total_verts_num;
/* One vertex is stored before and after as padding. */
total_verts_num += 1 + points.size() + 1;
/* Cyclic strokes have one extra vertex. */
total_verts_num += (is_cyclic ? 1 : 0);
…
total_triangles_num += (num_points + num_cyclic) * 2;
```

So one evaluated point ⇒ **one vertex ⇒ two triangles (one quad)**. A 109-point Catmull-Rom stroke
(§5.5) becomes 218 triangles; the same stroke as Poly with 10 points becomes 20.

The padding vertices at each end exist because the shader needs *adjacency*: it fetches `pos`, `pos1`,
`pos2`, `pos3` around each segment (`draw_grease_pencil_lib.glsl:457-460`). Padding vertices are marked
invalid with `mat = -1` (`:1553-1565`, and the global sentinels at `:1749-1753`).

### 9.2 Per-point payload

Vertex format (`:105-134`):

```cpp
struct GreasePencilStrokeVert {
  float pos[3], radius;                                    /* position + radius */
  int32_t mat, stroke_id, point_id, packed_asp_hard_rot;   /* ids + packed params */
  float uv_fill[2], u_stroke, opacity;
};
```

Filled by `populate_point()` (`:1426-1467`) from *interpolated* attributes:

```cpp
const VArray<float> radii        = attribute_interpolate<float>(info.drawing.radii(), curves);
const VArray<float> opacities    = attribute_interpolate<float>(info.drawing.opacities(), curves);
const VArray<float> rotations    = attribute_interpolate<float>(…"rotation"…, curves);
const VArray<ColorGeometry4f> vertex_colors = attribute_interpolate<ColorGeometry4f>(…, curves);
const VArray<float> miter_angles = interpolate_corners(curves);
```

and `attribute_interpolate` (`:1166-1176`) is:

```cpp
if (curves.is_single_type(CURVE_TYPE_POLY)) { return input; }   /* zero-copy fast path */
Array<T> out(curves.evaluated_points_num());
curves.interpolate_to_evaluated(VArraySpan(input), out.as_mutable_span());
```

— i.e. it routes straight back into the §3.3 four-way switch. **This is the single most important line
for understanding Grease Pencil curve types**: the same basis functions that shape the geometry shape
the thickness and colour.

Cyclicity is smuggled into the sign of `point_id` (`:1448-1449`) and decoded in
`decode_ma()` (`draw_grease_pencil_lib.glsl:172-188`). Aspect ratio, UV rotation, hardness and miter
angle are bit-packed into one int32 by `pack_rotation_aspect_hardness_miter()` (`:229-272`): 9 bits
aspect, 9 bits rotation (stored as $\cos\theta$ plus a sign bit, valid because $\theta\in[-90°,90°]$),
8 bits hardness, 6 bits miter.

### 9.3 The `u` (along-stroke) texture coordinate

`get_u_stroke()` (`:1495-1519`) uses `curves.evaluated_lengths_for_curve()` — arc length of the
**evaluated** polyline, i.e. a piecewise-linear approximation of the true spline arc length whose error
shrinks as $O(r^{-2})$ with resolution. Three placement modes exist for dot materials
(`GP_MATERIAL_PLACEMENT_COUNT / RADIUS / DENSITY`), and the RADIUS mode uses a rather pretty closed form,
`segment_radius_length()` (`:1237-1260`):

$$L = \frac{2\ln E_i}{\ln E},\qquad E = \frac{l+a}{l-a},\ E_i = \frac{a}{r_1}+1,\ a = r_2 - r_1$$

This counts how many tangentially-touching circles fit in a tapered segment: their radii form a
geometric progression, so the count is a ratio of logarithms.

### 9.4 Quad expansion in the vertex shader

`gpencil_vertex()` (`draw_grease_pencil_lib.glsl:424-…`) is invoked once per quad corner. Highlights:

* Attributes are fetched from a **buffer texture**, not vertex attributes
  (`texelFetch(gp_pos_tx, (stroke_point_id + k) * 3 + …)`), giving free adjacency access (`:457-467`).
* The quad corner is derived from `gl_VertexID` bits, avoiding a corner attribute entirely (`:525-526`):
  ```glsl
  float x = float(gl_VertexID & 1) * 2.0f - 1.0f; /* [-1..1] */
  float y = float(gl_VertexID & 2) - 1.0f;        /* [-1..1] */
  ```
* Cyclic joins are resolved by re-fetching across the stroke ends (`:496-511`).
* The frame is built screen-facing: $B = T \times \text{view}_z$, $N = \widehat{B \times T}$ (`:549-550`).
* Thickness is modulated by object scale and projection:
  ```glsl
  thickness = length(to_float3x3(drw_modelmat()) * float3(thickness * M_SQRT1_3));
  thickness *= drw_view().winmat[1][1] * viewport_res.y;
  ```
  (`:249-258`) — the $1/\sqrt3$ makes the scale factor the RMS of the three axes, an isotropic
  approximation for non-uniform object scale.
* Miter handling compares $\cos$ of the join angle against the decoded per-point limit and downgrades to
  bevel when exceeded (`:672-690`). This is where the per-type `miter_angle` interpolation from §6.9
  finally pays off.

### 9.5 The fragment side

`gpencil_stroke_segment_mask()` (`:62-146`) computes a signed distance to the poly-line segment and
applies a hardness falloff. The three-segment stencil (`p0→p1→p2→p3`) lets it produce round, bevel or
miter joins analytically per pixel, with the sharp/bevel branches selected by the same miter limits.
Because it is a *distance field* over the segment, the visual smoothness of a stroke is essentially
independent of how many evaluated points there are — a low-resolution Bézier still has smooth *edges*,
it just has a more polygonal *centreline*.

### 9.6 Fills

`Drawing::triangles()` (`blenkernel/intern/grease_pencil.cc:644-676`) triangulates using
`curves.evaluated_positions()` and `curves.evaluated_points_by_curve()`, projected onto the
Newell plane (`update_triangle_and_offsets_cache`, `:474-…`), via `BLI_polyfill_calc_arena`. Higher
`resolution` on a curved stroke therefore also means a denser fill mesh.

### 9.7 Wireframe

`grease_pencil_wire_batch_ensure()` (`:1768-1855`) builds a `GPU_PRIM_LINE_STRIP` index buffer over the
same evaluated vertices, using `gpu::RESTART_INDEX` between strokes and to blank out onion frames.

---

## 10. Edit-mode overlay drawing, per curve type

`grease_pencil_edit_batch_ensure()` (`:788-1164`) is the one place where curve type genuinely changes the
*overlay* topology:

| Buffer | Content | Type-dependence |
|---|---|---|
| `edit_points_pos` | **control** points (`curves.positions()`), + 2 extra per Bézier point for handles | Bézier adds `2 × bezier_points` entries (`:880`, `:1023-1040`) |
| `edit_line_pos` | **evaluated** points for non-NURBS curves, **control** points for NURBS | `:936-938` vs `grease_pencil_cache_add_nurbs` `:419-455` |
| `edit_points_info` | per-point bit flags: NURBS control point / Bézier handle / Bézier knot / handle types | `:43-49`, `bezier_data_value()` `:753-757` |
| `edit_handles_ibo` | line pairs knot↔handle | Bézier only (`:729-751`) |

The `edit_points_info` packing is documented at `:80-89`:

```
| left handle type | right handle type |      | BEZIER|  NURBS|
| 7              6 | 5               4 | 3  2 |     1 |     0 |
```

Selection is transferred from control points to the evaluated line by pushing a float mask through
`interpolate_to_evaluated` (`:963-974`) — so on a Bézier or Catmull-Rom stroke, the "selected" highlight
fades along the curve exactly as the geometry interpolates. Poly gets a masked fill instead.

The overlay passes themselves live in `draw/engines/overlay/overlay_grease_pencil.hh:129-156`
(`curve_edit_line`, `curve_edit_handles`, `curve_edit_points` shaders), with the handle pass expanded
`draw_expand(geom, GPU_PRIM_TRIS, 8, 1, …)` (`:203`) to build handle glyphs.

---

## 11. Type conversion mathematics

**Files:** `source/blender/geometry/intern/set_curve_type.cc`,
`source/blender/editors/grease_pencil/intern/grease_pencil_edit.cc:4719-4826`,
`source/blender/editors/sculpt_paint/grease_pencil/paint.cc:1642-1672`

### 11.1 Catmull-Rom → Bézier (exact)

`set_curve_type.cc:55-88`:

```cpp
/* Catmull Rom curves are the same as Bezier curves with automatically defined handle positions.
 * This constant defines the portion of the distance between the next/previous points to use for
 * the length of the handles. */
constexpr float handle_scale = 1.0f / 6.0f;
…
const float3 right_offset = src_positions[i + 1] - src_positions[i - 1];
dst_handles_r[i] = src_positions[i] + right_offset * handle_scale;
```

**Why 1/6 is exact.** A cubic Hermite segment with endpoint tangents $m_i, m_{i+1}$ equals the cubic
Bézier with control points

$$P_i,\quad P_i + \tfrac{m_i}{3},\quad P_{i+1} - \tfrac{m_{i+1}}{3},\quad P_{i+1}.$$

Catmull-Rom's tangent is $m_i = \tfrac12(P_{i+1}-P_{i-1})$, hence

$$R_i = P_i + \frac{1}{3}\cdot\frac{P_{i+1}-P_{i-1}}{2} = P_i + \frac{P_{i+1}-P_{i-1}}{6}.$$

So a Catmull-Rom → Bézier conversion is **lossless**, and the handles are set to `BEZIER_HANDLE_ALIGN`
(`:260-263`) so subsequent edits preserve $C^1$.

### 11.2 Bézier / Catmull-Rom → NURBS

`bezier_positions_to_nurbs()` (`:43-52`) simply lays out `[L_i, P_i, R_i]` as three NURBS control points
per Bézier point, and `to_nurbs_size()` (`:204-212`) returns `src_size * 3`. Combined with the default
order 4 and BEZIER-ish knots this reproduces the Bézier exactly (a cubic Bézier *is* a NURBS with
$k=4$ and clamped knots). Catmull-Rom reuses this by first converting to handles (`:90-100`).

### 11.3 NURBS → Bézier

`is_nurbs_to_bezier_one_to_one()` (`:24-30`) documents the geometric insight:

> for 3rd degree NURBS curves there is one-to-one relation with 3rd degree Bezier curves that can be
> exploited for conversion — Bezier handles sit on NURBS hull segments and in the middle between those
> handles are Bezier anchor points.

For NORMAL/ENDPOINT modes, handles are placed at $\tfrac13$/$\tfrac23$ along the control-hull segments
(`create_nurbs_to_bezier_handles`, `:102-168`) and anchors at the midpoints of consecutive handles
(`create_nurbs_to_bezier_positions`, `:170-185`). For Bezier knot modes, every 3rd NURBS point (offset 1)
is an anchor (`scale_input_assign(nurbs_positions, 3, 1, …)`).

Point-count bookkeeping (`to_bezier_size`, `:187-202`):

```cpp
case CURVE_TYPE_NURBS:
  if (is_nurbs_to_bezier_one_to_one(knots_mode)) {
    return cyclic ? src_size : std::max(1, src_size - 2);
  }
  return (src_size + 1) / 3;
```

### 11.4 Anything → Poly

`convert_to_poly()` (`grease_pencil_edit.cc:4768-4783`) calls
`geometry::resample_to_evaluated()` (`geometry/intern/resample_curves.cc:562-…`), which literally bakes
the evaluated points into control points and sets the type to Poly. Lossless with respect to *what is
drawn*, lossy with respect to editability.

### 11.5 Poly → anything (curve fitting)

Since a painted Grease Pencil stroke is a dense polyline, converting it to a smooth type would produce
one control point per sample. So the editors first *fit*:

```cpp
static bke::CurvesGeometry fit_poly_curves(bke::CurvesGeometry &curves,
                                           const IndexMask &selection, const float threshold)
{
  const VArray<float> thresholds = VArray<float>::from_single(threshold, curves.curves_num());
  const VArray<bool> corners = VArray<bool>::from_single(false, curves.points_num());
  return geometry::fit_poly_to_bezier_curves(curves, selection, thresholds, corners,
                                             geometry::FitMethod::Refit, {});
}
```

(`grease_pencil_edit.cc:4719-4728`; the fitter lives in `geometry/intern/fit_curves.cc`, interface in
`GEO_fit_curves.hh` with `FitMethod::Refit` = iterative knot removal and `FitMethod::Split` = recursive
least-squares.)

Interestingly, `convert_to_catmull_rom()` (`:4730-4766`) does **not** fit — it resamples to evaluated,
then simplifies with a Ramer–Douglas–Peucker-style `geometry::simplify_curve_attribute`, with an honest
comment:

```cpp
/* To avoid having too many control points, simplify the position attribute based on the
 * threshold. This doesn't replace an actual curve fitting (which would be better), but
 * is a decent approximation for the meantime. */
```

The paint tool can convert on stroke-completion too (`paint.cc:1642-1672`, gated on
`settings->curve_type != CURVE_TYPE_POLY` at `:1755`), using
`fit_poly_to_bezier_curves` followed by an optional Bézier→CR/NURBS conversion.

### 11.6 Export

* **SVG** (`io/grease_pencil/intern/grease_pencil_io_export_svg.cc:299-320`) converts CR/NURBS to Bézier
  and emits real `C` path commands (`:492-510`) — so SVG output is resolution-independent.
* **PDF** (`…_export_pdf.cc:147-162`) instead calls `resample_to_evaluated()` — PDF output is a polyline
  at whatever `resolution` was set.

---

## 12. Numerical and performance notes

1. **Caching topology.** `evaluated_offsets_cache`, `nurbs_basis_cache`, `evaluated_position_cache`,
   `evaluated_tangent_cache`, `evaluated_normal_cache`, `evaluated_length_cache` are all `SharedCache`s.
   Moving a control point invalidates positions but **not** the NURBS basis cache or the offsets cache
   (`tag_positions_changed` vs `tag_topology_changed`), which is what makes dragging a NURBS point cheap.

2. **The Poly fast paths are everywhere** and worth listing, because they are the hot path for Grease
   Pencil: `curves_geometry.cc:709-716` (offsets), `:831-836` (positions), `:1114-1119` (tilt),
   `:1401-1404` (bounds), `draw_cache_impl_grease_pencil.cc:1169-1171` and `:1182-1185` (attributes),
   `:963-967` (selection), `curve_bezier.cc` not involved at all.

3. **Grain sizes are tuned per workload**: 128 curves for position evaluation, 512 for Catmull-Rom
   segments, 4096 for NURBS spans, `max(eval/points*32, 1)` adaptive for Bézier segments, 1024/4096 for
   handle solving.

4. **Precision choices**
   * Catmull-Rom writes segment starts by assignment, not evaluation (§5.3).
   * Bézier uses forward differencing — fast, mildly error-accumulating (§6.5).
   * NURBS uses the numerically stable A2.2 recursion with no `0/0` (§7.6).
   * Poly tangents avoid catastrophic cancellation via a cross-product reformulation, with a bug
     reference `#146332` (§8.1).
   * Minimum-twist normals re-normalise every step because *"the iterative process here can accumulate
     small floating point errors, leading to 'not enough' normalized results at some point (see
     #121169)"* (`curve_poly.cc:151-155`).

5. **Cost model for a Grease Pencil stroke.** With $n$ control points and resolution $r$:
   evaluated points $\approx rn$, vertices $= rn + 2\,(+1)$, triangles $= 2rn$. Raising `resolution` from
   12 to 32 nearly triples GPU vertex load for Catmull-Rom/Bézier/NURBS strokes — and does nothing at
   all for Poly.

6. **Degenerate inputs are handled, never asserted away**: 1- and 2-point Catmull-Rom (§5.4),
   single-point Bézier (`curve_bezier.cc:41-45`, `:350-353`), invalid NURBS (§7.3), all-coincident poly
   points (`curve_poly.cc:96-102`).

---

## 13. Quick reference: file / function map

### Evaluation core

| File | Key symbols |
|---|---|
| `makesdna/DNA_curves_types.h` | `CurveType` :29, `HandleType` :62, `KnotsMode` :75, `NormalMode` :84 |
| `blenkernel/BKE_curves.hh` | `BasisCache` :50, `segments_num` :574, `poly` ns :608, `bezier` ns :639, `catmull_rom` ns :809 (`calculate_basis` :833, `interpolate` :842), `nurbs` ns :863, bezier inlines :1113, `knots_num` :1156, `control_points_num` :1162 |
| `blenkernel/intern/curves_geometry.cc` | `calculate_evaluated_offsets` :649, `evaluated_points_by_curve` :706, `ensure_nurbs_basis_cache` :764, `evaluated_positions` :829, `evaluated_tangents` :918, `EvalData` :999, `evaluate_generic_data_for_curve` :1010, `evaluated_normals` :1041, `interpolate_to_evaluated` :1136/:1156, `ensure_evaluated_lengths` :1183 |
| `blenkernel/intern/curve_poly.cc` | `delta_dir` :16, `direction_bisect` :33, `calculate_tangents` :74, `calculate_normals_z_up` :126, `calculate_normals_minimum` :166 |
| `blenkernel/intern/curve_catmull_rom.cc` | `calculate_evaluated_num` :16, `calculate_basis` :28, `evaluate_segment` :42, `interpolate_to_evaluated` :57/:114/:132/:147 |
| `blenkernel/intern/curve_bezier.cc` | `calculate_evaluated_offsets` :33, `insert` (de Casteljau) :69, `calculate_aligned_handle` :89, `calculate_align_both_handles` :102, `calculate_point_handles` :146, `calculate_auto_handles` :263, `evaluate_segment_ex` :305, `calculate_evaluated_positions` :347, `linear_interpolation` :400 |
| `blenkernel/intern/curve_nurbs.cc` | `check_valid_eval_params` :16, `calc_nonzero_knot_spans` :44, `is_breakpoint` :67, `count_nonzero_knot_spans` :72, `calculate_evaluated_num` :88, `calculate_knots` :121, `load_curve_knots` :165, `calculate_multiplicity_sequence` :183, `calculate_basis_for_point` :205, `calculate_basis_cache` :237, `interpolate_to_evaluated(_rational)` :316/:336/:358 |
| `blenkernel/intern/curves_utils.cc` | `foreach_curve_by_type` :100 |
| `blenkernel/BKE_attribute_math.hh` | `mix2` :64, `mix4` :217, `SimpleMixer` :536 |

### Grease Pencil drawing

| File | Key symbols |
|---|---|
| `draw/intern/draw_cache_impl_grease_pencil.cc` | vertex formats :105-150, `pack_rotation_aspect_hardness_miter` :229, non-NURBS masks :282/:301, `index_buf_add_line_points` :318, NURBS masks :375/:398, `grease_pencil_cache_add_nurbs` :419, `index_buf_add_nurbs_lines` :457, `grease_pencil_weight_batch_ensure` :487, bezier handle IBO :729/:759, `grease_pencil_edit_batch_ensure` :788, `attribute_interpolate` :1166, `interpolate_corners` :1178, `segment_radius_length` :1237, `grease_pencil_geom_batch_ensure` :1280, `populate_point` :1426, `populate_curve` :1469, `grease_pencil_wire_batch_ensure` :1768 |
| `draw/intern/shaders/draw_grease_pencil_lib.glsl` | `gpencil_stroke_hardess_mask` :27, `gpencil_stroke_segment_mask` :62, `decode_ma` :177, decoders :213-242, `gpencil_stroke_thickness_modulate` :249, `dot_segment` :283, `gpencil_vertex` :424 |
| `draw/engines/gpencil/shaders/gpencil_vert.glsl` | `main` :35 |
| `draw/engines/gpencil/gpencil_shader_shared.hh` | `GP_IS_STROKE_VERTEX_BIT` :44, `GP_VERTEX_ID_SHIFT` :45, corner-type bits :46-48 |
| `draw/engines/overlay/overlay_grease_pencil.hh` | edit passes :129-156, draws :190-212 |
| `blenkernel/intern/grease_pencil.cc` | triangulation :474/:644, Newell normals :679/:720 |

### Conversion / IO

| File | Key symbols |
|---|---|
| `geometry/intern/set_curve_type.cc` | `is_nurbs_to_bezier_one_to_one` :24, `bezier_positions_to_nurbs` :43, `catmull_rom_to_bezier_handles` :55, `catmull_rom_to_nurbs_positions` :90, `create_nurbs_to_bezier_handles` :102, `to_bezier_size` :187, `to_nurbs_size` :204, `convert_curves_to_bezier` :215 |
| `geometry/intern/resample_curves.cc` | `resample_to_evaluated` :562 |
| `geometry/GEO_fit_curves.hh` | `FitMethod` :11, `fit_poly_to_bezier_curves` :34 |
| `editors/grease_pencil/intern/grease_pencil_edit.cc` | `fit_poly_curves` :4719, `convert_to_catmull_rom` :4730, `convert_to_poly` :4768, `convert_to_bezier` :4784, `convert_to_nurbs` :4806, operator :4831 |
| `editors/sculpt_paint/grease_pencil/paint.cc` | `sample_curve_2d` :97, `convert_stroke_type` :1642, call site :1755 |
| `io/grease_pencil/.../export_svg.cc` | CR/NURBS→Bézier :299-320, path emit :472-510 |
| `io/grease_pencil/.../export_pdf.cc` | resample-to-evaluated :147-162 |

### Tests

`blenkernel/intern/curves_geometry_test.cc` — `NURBSEvaluation` :456, zero-order fallbacks :544/:564,
`BasisCacheBezierSegmentDeg2` :589, `BasisCacheNonUniformDeg2` :626, knot-vector tests :705-820.

---

## Appendix A — worked numeric examples

*(All figures below were produced by re-implementing the exact code paths and cross-checking against
textbook formulas.)*

### A.1 Catmull-Rom basis vs textbook

Control points $P_0=(0,0),P_1=(1,1),P_2=(2,0),P_3=(3,1)$, evaluating the $P_1\to P_2$ segment:

| $t$ | Blender `0.5 * mix4(calculate_basis(t), …)` | Textbook $\tfrac12[\dots]$ | Δ |
|---|---|---|---|
| 0.00 | (1.000000, 1.00000) | same | 0 |
| 0.25 | (1.250000, 0.84375) | same | 0 |
| 0.50 | (1.500000, 0.50000) | same | 0 |
| 0.75 | (1.750000, 0.15625) | same | 0 |
| 1.00 | (2.000000, 0.00000) | same | 0 |

### A.2 Bézier forward differencing vs Bernstein

$P_0=(0,0,0)$, $P_1=(1,2,0)$, $P_2=(3,2,0)$, $P_3=(4,0,0)$, 8 samples: maximum absolute difference
between `evaluate_segment_ex` and direct Bernstein evaluation = **0.0** (bit-identical at this length).

### A.3 Evaluated-point counts, `resolution = 12`

| Curve | `n` | cyclic | evaluated points |
|---|---|---|---|
| Poly | 10 | – | 10 |
| Catmull-Rom | 10 | no | 109 |
| Catmull-Rom | 10 | yes | 120 |
| Bézier, all AUTO | 10 | no | 109 |
| Bézier, all VECTOR | 10 | no | 10 |
| Bézier, 5 AUTO + 4 VECTOR segs | 10 | no | 5·12 + 4·1 + 1 = 65 |
| NURBS order 4 NORMAL | 6 | no | 37 |
| NURBS order 4 NORMAL | 6 | yes | 72 |
| NURBS order 4 ENDPOINT | 6 | yes | 48 |
| NURBS order 4 BEZIER | 6 | no | 13 |

---

## Appendix B — knot vector tables

Generated by executing the exact logic of `curve_nurbs.cc:121-163` and `:44-65`
(`points_num = 6`, `order = 4`, `resolution = 12`):

| Mode | Cyclic | Knot count | Knots | Non-zero spans | Evaluated |
|---|---|---|---|---|---|
| NORMAL | no | 10 | `0 1 2 3 4 5 6 7 8 9` | 3 | 37 |
| NORMAL | yes | 13 | `0 1 2 3 4 5 6 7 8 9 10 11 12` | 6 | 72 |
| ENDPOINT | no | 10 | `0 0 0 0 1 2 3 3 3 3` | 3 | 37 |
| ENDPOINT | yes | 13 | `0 1 1 1 2 3 4 5 5 5 6 7 8` | 4 | 48 |
| BEZIER | no | 10 | `0 0 1 1 1 2 2 2 3 3` | 1 | 13 |
| BEZIER | yes | 13 | `0 0 1 1 1 2 2 2 3 3 3 4 4` | 2 | 24 |
| ENDPOINT_BEZIER | no | 10 | `0 0 0 0 1 1 1 1 1 1` | 1 | 13 |
| ENDPOINT_BEZIER | yes | 13 | `0 1 1 1 2 2 2 3 3 3 4 4 4` | 2 | 24 |

Multiplicity sequences pinned by `KnotVectorTest` (`curves_geometry_test.cc:705-820`), e.g. for
`order = 5, points = 7, NORMAL`: `[1,1,1,1,1,1,1,1,1,1,1,1]` — a fully uniform vector.

---

### Closing observation

The design is worth stating explicitly because it explains almost every behaviour a Grease Pencil user
observes:

> **Grease Pencil never draws a curve. It draws a polyline, and each curve type is just a different
> recipe for producing that polyline.**

Everything follows: why `resolution` changes triangle count but not stroke smoothness at the edges
(§9.5); why NURBS strokes need a separate control-polygon overlay (§7.9); why Bézier thickness ramps
look "wrong" on fast segments (§6.7); why Catmull-Rom radius can dip below the authored minimum (§5.7);
why a Poly stroke is essentially free (§12.2); and why converting to Poly is visually lossless while
converting *from* Poly needs a curve fitter (§11.4-11.5).
