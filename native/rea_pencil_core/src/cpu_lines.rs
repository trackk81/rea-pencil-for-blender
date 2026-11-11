use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::{panic::catch_unwind, ptr, slice};

type Vec3 = [f32; 3];
type Clip = [f32; 4];

#[derive(Clone, Copy)]
struct Style {
    flags: u32,
    min_cos: f32,
    max_cos: f32,
    radius: f32,
    aa: f32,
    rgba: [f32; 4],
    max_stroke_size: f32,
    wave: Vec3,
    dash: Vec3,
}

#[repr(C)]
struct ReductionInput {
    range_factor: Vec3,
    enabled: u32,
    reference: Vec3,
    curve: *const f32,
}

struct Reduction {
    range_factor: Vec3,
    reference: Vec3,
    curve: Option<Vec<f32>>,
}

impl Reduction {
    fn factor(&self, point: Vec3) -> f32 {
        let [near, far, factor] = self.range_factor;
        let delta = minus(point, self.reference);
        let t = ((dot(delta, delta).sqrt() - near) / (far - near).max(1e-6)).clamp(0., 1.);
        if let Some(curve) = &self.curve {
            sample_curve(curve, t)
        } else {
            1. + (factor - 1.) * t
        }
    }

    fn max_factor(&self) -> f32 {
        if self.curve.is_some() { 1. } else { self.range_factor[2].max(1.) }
    }
}

fn sample_curve(curve: &[f32], t: f32) -> f32 {
    let sample = t.clamp(0., 1.) * 4096.;
    let i = sample as usize;
    let f = sample - i as f32;
    curve[i] * (1. - f) + curve[(i + 1).min(4096)] * f
}

#[repr(C)]
pub struct StyleInput {
    flags: u32,
    min_cos: f32,
    max_cos: f32,
    radius: f32,
    aa: f32,
    rgba: [f32; 4],
    stamp: *const f32,
    stamp_width: usize,
    stamp_height: usize,
    stamp_period: f32,
    reduction: [ReductionInput; 2],
    stroke_curves: [*const f32; 2],
    wave: Vec3,
    dash: Vec3,
}

#[repr(C)]
pub struct Mesh {
    vertices: *const Vec3,
    vertex_count: usize,
    triangles: *const [u32; 3],
    triangle_count: usize,
    edges: *const [u32; 2],
    edge_count: usize,
    loop_edges: *const u32,
    loop_vertices: *const u32,
    loop_count: usize,
    sizes: *const u32,
    normals: *const Vec3,
    face_count: usize,
    projection: [[f32; 4]; 4],
    normal: [Vec3; 3],
    eye: Vec3,
    perspective: u32,
    handedness: f32,
    owner: u32,
    style_ids: *const u32,
    style_count: usize,
    marks: *const u8,
    world: [[f32; 4]; 3],
    face_masks: *const *const u8,
    face_materials: *const i32,
}

pub struct Frame {
    width: usize,
    height: usize,
    styles: Vec<Style>,
    stamps: Vec<Option<Stamp>>,
    reductions: Vec<[Option<Reduction>; 2]>,
    stroke_curves: Vec<[Option<Vec<f32>>; 2]>,
    // A single ordered atomic couples nonnegative depth bits to the winning
    // object's ID. Equal depths choose the smaller ID on every worker count.
    depth: Vec<AtomicU64>,
}

#[derive(Clone, Copy)]
struct Stroke {
    a: Vec3,
    b: Vec3,
    outside: f32,
    directed_wave_side: f32,
    owner: u32,
    exterior_only: bool,
}

pub struct Ink {
    strokes: Vec<Vec<Stroke>>,
    stamp_spans: Vec<Vec<StampSpan>>,
    reduction_points: Vec<Vec<[Clip; 2]>>,
}

struct Stamp {
    coverage: Vec<f32>,
    width: usize,
    height: usize,
    period: f32,
}

impl Stamp {
    fn sample(&self, along: f32, cross: f32, diameter: f32) -> f32 {
        let v = cross/diameter+0.5;
        if !(0.0..=1.0).contains(&v) { return 0.0; }
        let phase = along/self.period;
        let x = (phase-phase.floor())*self.width as f32-0.5;
        let y = v*self.height as f32-0.5;
        let fx = x-x.floor();
        let fy = y-y.floor();
        let x0 = (x.floor() as isize).rem_euclid(self.width as isize) as usize;
        let x1 = (x0+1)%self.width;
        let y0 = (y.floor() as isize).clamp(0,self.height as isize-1) as usize;
        let y1 = ((y.floor() as isize)+1).clamp(0,self.height as isize-1) as usize;
        let low = self.coverage[y0*self.width+x0]*(1.0-fx)+self.coverage[y0*self.width+x1]*fx;
        let high = self.coverage[y1*self.width+x0]*(1.0-fx)+self.coverage[y1*self.width+x1]*fx;
        low*(1.0-fy)+high*fy
    }
}

#[derive(Clone, Copy)]
struct StampSpan {
    start: f32,
    end: f32,
    caps: [bool; 2],
    total: f32,
    next: Option<usize>,
    directed_wave_side: f32,
}

struct WaveRun {
    bounds: [f32; 2],
    phase_origin: f32,
}

const CUT_VERTEX: u32 = u32::MAX;

fn stamp_spans(lines: &[Stroke], endpoints: &[[u32; 2]], vertex_count: usize, wave_direction: Option<f32>, counterclockwise: bool, loop_start: u32) -> Vec<StampSpan> {
    const NONE: usize = usize::MAX;
    const BRANCH: usize = usize::MAX-1;
    let mut links = vec![[NONE; 2]; vertex_count];
    for (index, ends) in endpoints.iter().enumerate() {
        for (side, &vertex) in ends.iter().enumerate() {
            if vertex == CUT_VERTEX { continue; }
            let slots = &mut links[vertex as usize];
            let half = 2*index+side;
            if slots[0] == NONE { slots[0] = half; }
            else if slots[1] == NONE { slots[1] = half; }
            else { slots[1] = BRANCH; }
        }
    }
    let neighbor = |vertex: u32, half: usize| {
        if vertex == CUT_VERTEX { return NONE; }
        let [first, second] = links[vertex as usize];
        if second >= BRANCH { NONE } else if first == half { second } else { first }
    };
    let empty = StampSpan {start:f32::NAN,end:0.0,caps:[true;2],total:0.,next:None,directed_wave_side:0.};
    let mut spans = vec![empty; lines.len()];
    let (sin, cos) = wave_direction.unwrap_or(DEFAULT_WAVE_DIRECTION).sin_cos();
    let mut chain = Vec::new();
    let mut walk = |mut half: usize| {
        let first_half = half;
        chain.clear();
        let mut distance = 0.0;
        while half != NONE {
            let edge = half/2;
            let side = half%2;
            if !spans[edge].start.is_nan() { break; }
            let [a,b] = endpoints[edge];
            let caps = [neighbor(a,2*edge)==NONE, neighbor(b,2*edge+1)==NONE];
            let delta = minus(lines[edge].b,lines[edge].a);
            let length = (delta[0]*delta[0]+delta[1]*delta[1]).sqrt();
            let end = distance+length;
            if let Some(&previous) = chain.last() {
                let previous: &mut StampSpan = &mut spans[previous];
                previous.next = Some(edge);
            }
            spans[edge] = if side==0 { StampSpan {start:distance,end,caps,total:0.,next:None,directed_wave_side:0.} }
                          else { StampSpan {start:end,end:distance,caps,total:0.,next:None,directed_wave_side:0.} };
            chain.push(edge);
            distance = end;
            half = neighbor(endpoints[edge][1-side],2*edge+1-side);
        }
        let Some(&head) = chain.first() else { return; };
        let tail = *chain.last().unwrap();
        let start_side = usize::from(spans[head].start > spans[head].end);
        let end_side = usize::from(spans[tail].start < spans[tail].end);
        let start_vertex = endpoints[head][start_side];
        let end_vertex = endpoints[tail][end_side];
        let terminal = |vertex: u32| vertex != CUT_VERTEX && links[vertex as usize][1] == NONE;
        let open_directed_wave = terminal(start_vertex) && terminal(end_vertex)
            && chain.iter().all(|&edge| lines[edge].directed_wave_side != 0.);
        if open_directed_wave {
            let start = if start_side == 0 { lines[head].a } else { lines[head].b };
            let end = if end_side == 0 { lines[tail].a } else { lines[tail].b };
            // Convert the authored image Y-down angle to the Y-up raster.
            let delta = minus(end, start);
            let projection = delta[0] * cos - delta[1] * sin;
            let threshold = if wave_direction.is_some() { 0.00005 } else { -0.00005 };
            if projection < threshold {
                chain.reverse();
                for (position, &edge) in chain.iter().enumerate() {
                    let span = &mut spans[edge];
                    span.start = distance - span.start;
                    span.end = distance - span.end;
                    span.next = chain.get(position + 1).copied();
                }
            }
        }
        let closed_directed_wave = wave_direction.is_some() && half == first_half
            && chain.iter().all(|&edge| lines[edge].directed_wave_side != 0.);
        if closed_directed_wave {
            let mut lo = [f32::INFINITY; 2];
            let mut hi = [f32::NEG_INFINITY; 2];
            let origin = lines[head].a;
            let mut area = 0.;
            for &edge in &chain {
                let line = lines[edge];
                for k in 0..2 {
                    lo[k] = lo[k].min(line.a[k]).min(line.b[k]);
                    hi[k] = hi[k].max(line.a[k]).max(line.b[k]);
                }
                let a = minus(line.a, origin);
                let b = minus(line.b, origin);
                let cross = a[0]*b[1]-a[1]*b[0];
                area += if spans[edge].start < spans[edge].end { cross } else { -cross };
            }
            let axis = [cos*(hi[0]-lo[0]), -sin*(hi[1]-lo[1])];
            let reverse = (area > 0.) != counterclockwise;
            let score = |edge: usize| {
                let line = lines[edge];
                let forward = (spans[edge].start < spans[edge].end) != reverse;
                let [x,y,_] = if forward { line.a } else { line.b };
                match loop_start {
                    1 => [x,y],
                    2 => [-x,y],
                    3 => [y,x],
                    4 => [-y,x],
                    _ => [(line.a[0]+line.b[0])*axis[0]+(line.a[1]+line.b[1])*axis[1],0.],
                }
            };
            if reverse { chain.reverse(); }
            let anchor = (0..chain.len()).min_by(|&a,&b| {
                let [ax,ay] = score(chain[a]);
                let [bx,by] = score(chain[b]);
                ax.total_cmp(&bx).then(ay.total_cmp(&by)).then(chain[a].cmp(&chain[b]))
            }).unwrap();
            chain.rotate_left(anchor);
            let mut position = 0.;
            for (index, &edge) in chain.iter().enumerate() {
                let span = &mut spans[edge];
                let length = (span.end-span.start).abs();
                let forward = (span.start < span.end) != reverse;
                let end = position + length;
                (span.start, span.end) = if forward { (position, end) } else { (end, position) };
                span.next = chain.get(index+1).copied();
                position = end;
            }
            distance = position;
        }
        for &edge in &chain {
            spans[edge].total = distance;
            if open_directed_wave || closed_directed_wave {
                spans[edge].directed_wave_side = lines[edge].directed_wave_side;
            }
        }
    };
    // Open strokes start at the lower terminal vertex, matching the GPU's
    // default orientation. A branch is a terminal for each of its edges.
    let mut terminals = Vec::new();
    for (index, ends) in endpoints.iter().enumerate() {
        for (side, &vertex) in ends.iter().enumerate() {
            let half = 2*index+side;
            if neighbor(vertex,half)==NONE { terminals.push((vertex,half)); }
        }
    }
    terminals.sort_unstable();
    for (_, half) in terminals { walk(half); }
    for &[first, _] in &links {
        if first != NONE { walk(first); }
    }
    spans
}

#[cfg(test)]
mod chain_tests {
    use super::*;

    fn line(a: [f32; 2], b: [f32; 2]) -> Stroke {
        Stroke { a:[a[0],a[1],0.5], b:[b[0],b[1],0.5], outside:1.0, directed_wave_side:0.,
                 owner:1, exterior_only:true }
    }

    #[test]
    fn reversed_edges_preserve_continuous_distance_from_terminal() {
        let lines = [line([3.,4.],[3.,0.]), line([0.,0.],[3.,0.])];
        let spans = stamp_spans(&lines, &[[2,1],[0,1]], 3, None, false, 0);
        assert_eq!((spans[1].start,spans[1].end), (0.,3.));
        assert_eq!((spans[0].start,spans[0].end), (7.,3.));
        assert_eq!(spans[1].caps,[true,false]);
        assert_eq!(spans[0].caps,[true,false]);
    }

    #[test]
    fn perspective_marked_wave_reverses_at_projected_terminal() {
        let mut lines = [line([0.,0.],[0.,-3.]), line([0.,-3.],[0.,-7.])];
        for line in &mut lines { line.directed_wave_side = -1.; }
        let spans = stamp_spans(&lines, &[[0,1],[1,2]], 3, None, false, 0);
        assert_eq!((spans[1].start, spans[1].end, spans[1].next), (4., 0., Some(0)));
        assert_eq!((spans[0].start, spans[0].end, spans[0].next), (7., 4., None));
        assert_eq!(spans[0].directed_wave_side, -1.);
    }

    #[test]
    fn custom_wave_angle_changes_the_open_chain_start() {
        let mut lines = [line([0.,0.],[1.,2.]), line([1.,2.],[2.,4.])];
        for line in &mut lines { line.directed_wave_side = -1.; }
        let endpoints = [[0,1],[1,2]];
        let forward = stamp_spans(&lines, &endpoints, 3, Some(0.), false, 0);
        let reverse = stamp_spans(&lines, &endpoints, 3, Some(std::f32::consts::FRAC_PI_6), false, 0);
        assert_eq!(forward[0].start, 0.);
        assert_eq!(forward[0].next, Some(1));
        assert_eq!(reverse[1].end, 0.);
        assert_eq!(reverse[1].next, Some(0));
        assert_eq!(reverse[0].directed_wave_side, -1.);
    }

    #[test]
    fn closed_wave_winding_uses_opposite_ends_of_the_same_edge() {
        let mut lines = [line([0.,0.],[3.,0.]), line([3.,0.],[3.,4.]),
                         line([3.,4.],[0.,4.]), line([0.,4.],[0.,0.])];
        for line in &mut lines { line.directed_wave_side = 1.; }
        let endpoints = [[0,1],[1,2],[2,3],[3,0]];
        let clockwise = stamp_spans(&lines, &endpoints, 4, Some(0.), false, 0);
        let counterclockwise = stamp_spans(&lines, &endpoints, 4, Some(0.), true, 0);
        assert_eq!((clockwise[3].start, clockwise[3].end), (4., 0.));
        assert_eq!((counterclockwise[3].start, counterclockwise[3].end), (0., 4.));
        assert_eq!(clockwise[3].caps, [false, false]);
        assert_eq!(counterclockwise[3].caps, [false, false]);
    }

    #[test]
    fn closed_loop_accumulates_exactly_one_perimeter_without_caps() {
        let lines = [line([0.,0.],[3.,0.]),line([3.,0.],[3.,4.]),
                     line([3.,4.],[0.,4.]),line([0.,4.],[0.,0.])];
        let spans = stamp_spans(&lines,&[[0,1],[1,2],[2,3],[3,0]],4, None, false, 0);
        let mut previous = 0.;
        for span in spans {
            assert_eq!(span.start,previous);
            assert_eq!(span.caps,[false,false]);
            previous=span.end;
        }
        assert_eq!(previous,14.);
    }

    #[test]
    fn branches_and_clipped_endpoints_do_not_join_unrelated_runs() {
        let lines = [line([0.,0.],[1.,0.]),line([0.,0.],[0.,2.]),
                     line([0.,0.],[-3.,0.]),line([4.,0.],[6.,0.])];
        let spans = stamp_spans(&lines,&[[0,1],[0,2],[0,3],[CUT_VERTEX,CUT_VERTEX]],4, None, false, 0);
        for (span,length) in spans.iter().zip([1.,2.,3.,2.]) {
            assert_eq!(span.start.min(span.end),0.);
            assert_eq!((span.end-span.start).abs(),length);
            assert_eq!(span.caps,[true,true]);
        }
    }
}

const OUTLINE: u32 = 1;
const BORDER: u32 = 2;
const CREASE: u32 = 4;
const RESERVE_OUTLINE: u32 = 8;
const MARK: u32 = 16;
const EXCLUDE_MARK: u32 = 32;
const MATERIAL: u32 = 64;
const EXCLUDE_MATERIAL: u32 = 128;
const HIDDEN: u32 = 256;
// When set, dash[2] carries the Wave angle and the dash gap is zero.
const WAVE_DIRECTION: u32 = 512;
const WAVE_COUNTERCLOCKWISE: u32 = 1024;
const DEFAULT_WAVE_DIRECTION: f32 = -std::f32::consts::FRAC_PI_6;
const EMPTY: u64 = (1.0f32.to_bits() as u64) << 32 | u32::MAX as u64;
const DEPTH_BIAS: f32 = 0.000002;

#[inline]
fn dot(a: Vec3, b: Vec3) -> f32 {
    a[0]*b[0] + a[1]*b[1] + a[2]*b[2]
}

#[inline]
fn minus(a: Vec3, b: Vec3) -> Vec3 {
    [a[0]-b[0], a[1]-b[1], a[2]-b[2]]
}

#[inline]
fn lerp(a: Clip, b: Clip, t: f32) -> Clip {
    std::array::from_fn(|i| a[i]+(b[i]-a[i])*t)
}

#[inline]
fn outcode(p: Clip) -> u8 {
    (p[0] < -p[3]) as u8
        | ((p[0] > p[3]) as u8) << 1
        | ((p[1] < -p[3]) as u8) << 2
        | ((p[1] > p[3]) as u8) << 3
        | ((p[2] < -p[3]) as u8) << 4
        | ((p[2] > p[3]) as u8) << 5
}

#[inline]
fn screen(p: Clip, size: [f32; 2]) -> Vec3 {
    let half_w = 0.5/p[3];
    [(p[0]*half_w+0.5)*size[0], (p[1]*half_w+0.5)*size[1], p[2]*half_w+0.5]
}

#[inline]
fn range(low: f32, high: f32, limit: usize) -> std::ops::Range<usize> {
    let first = (low-0.5).ceil().max(0.0) as usize;
    let end = ((high-0.5).floor()+1.0).max(0.0) as usize;
    first.min(limit)..end.min(limit)
}

impl Frame {
    fn triangle(&self, a: Vec3, b: Vec3, c: Vec3, owner: u32) {
        let ab = minus(b, a);
        let ac = minus(c, a);
        let det = ab[0]*ac[1] - ab[1]*ac[0];
        if det.abs() < 1e-12 {
            return;
        }
        let xs = range(a[0].min(b[0]).min(c[0]), a[0].max(b[0]).max(c[0]), self.width);
        let ys = range(a[1].min(b[1]).min(c[1]), a[1].max(b[1]).max(c[1]), self.height);
        if xs.is_empty() || ys.is_empty() {
            return;
        }
        let u_x = ac[1]/det;
        let v_x = -ab[1]/det;
        let row_x = xs.start as f32 + 0.5 - a[0];
        for y in ys {
            let dy = y as f32 + 0.5 - a[1];
            let mut u = row_x*u_x - dy*ac[0]/det;
            let mut v = row_x*v_x + dy*ab[0]/det;
            for x in xs.clone() {
                if u >= -1e-6 && v >= -1e-6 && u+v <= 1.000001 {
                    let z = (a[2]+u*ab[2]+v*ac[2]).clamp(0.0,1.0);
                    let packed = (z.to_bits() as u64) << 32 | owner as u64;
                    let pixel = &self.depth[y*self.width+x];
                    if packed < pixel.load(Relaxed) {
                        pixel.fetch_min(packed, Relaxed);
                    }
                }
                u += u_x;
                v += v_x;
            }
        }
    }

    fn clipped_triangle(&self, corners: [Clip; 3], size: [f32; 2], owner: u32) {
        let mut points = [corners[0]; 8];
        points[..3].copy_from_slice(&corners);
        let mut n = 3;
        for sign in [1.0, -1.0] {
            if n == 0 { return; }
            let input = points;
            let mut previous = input[n-1];
            let old_n = n;
            n = 0;
            // Each near/far plane adds at most one corner to a convex polygon.
            for &current in &input[..old_n] {
                let a = previous[3]+previous[2]*sign;
                let b = current[3]+current[2]*sign;
                if (a < 0.0) != (b < 0.0) {
                    points[n] = lerp(previous,current,a/(a-b));
                    n += 1;
                }
                if b >= 0.0 {
                    points[n] = current;
                    n += 1;
                }
                previous = current;
            }
        }
        if n < 3 { return; }
        let a = screen(points[0],size);
        for i in 1..n-1 {
            self.triangle(a,screen(points[i],size),screen(points[i+1],size),owner);
        }
    }

    fn sample(&self, x: i32, y: i32) -> (f32, u32) {
        let x = (x.max(0) as usize).min(self.width-1);
        let y = (y.max(0) as usize).min(self.height-1);
        let value = self.depth[y*self.width+x].load(Relaxed);
        (f32::from_bits((value >> 32) as u32), value as u32)
    }

    fn visible(&self, center: Vec3, outward: [f32; 2], owner: u32) -> bool {
        center[2] <= self.visibility_limit(center, outward, owner)
    }

    fn visibility_limit(&self, center: Vec3, outward: [f32; 2], owner: u32) -> f32 {
        let [x,y,z] = center;
        let ix = (x-0.5).floor() as i32;
        let iy = (y-0.5).floor() as i32;
        let fx = x-0.5-ix as f32;
        let fy = y-0.5-iy as f32;
        // A contour crossing its own depth samples should keep its ink. A
        // different mesh remains an occluder, including inside the AA fringe.
        let correction = outward != [0.0; 2]
            && self.sample((x+1.5*outward[0]).floor() as i32,
                           (y+1.5*outward[1]).floor() as i32).1 != owner;
        let tap = |px: i32, py: i32| {
            let (depth, object) = self.sample(px,py);
            if correction && object == owner
                && (px as f32+0.5-x)*outward[0]+(py as f32+0.5-y)*outward[1] < 0.0 {
                z
            } else {
                depth
            }
        };
        let low = tap(ix,iy)*(1.0-fx)+tap(ix+1,iy)*fx;
        let high = tap(ix,iy+1)*(1.0-fx)+tap(ix+1,iy+1)*fx;
        low*(1.0-fy)+high*fy+DEPTH_BIAS
    }

    fn stroke(&self, line: Stroke, index: usize, rect: [usize; 4], output: &mut [[f32; 4]],
              span: Option<&StampSpan>, world: Option<&[Clip; 2]>, wave_runs: Option<&[WaveRun]>) {
        let style = self.styles[index];
        let reduction = &self.reductions[index];
        let profiles = &self.stroke_curves[index];
        let stamp = self.stamps[index].as_ref();
        let [left,bottom,right,top] = rect;
        let a = line.a;
        let d = minus(line.b, a);
        let length_sq = d[0]*d[0]+d[1]*d[1];
        if length_sq <= 1e-10 { return; }
        let inv_length = length_sq.sqrt().recip();
        let outward = [-d[1]*inv_length*line.outside,d[0]*inv_length*line.outside];
        let aa = style.aa.min(1.0);
        let max_radius = style.radius * style.max_stroke_size * reduction[0].as_ref().map_or(1., Reduction::max_factor);
        let reach = max_radius+aa*0.5;
        let length = length_sq * inv_length;
        let axis = usize::from(d[0].abs() < d[1].abs());
        let cross = 1-axis;
        let lower = [left,bottom];
        let upper = [right,top];
        let width = right-left;
        let scan_reach = reach + style.wave[0].abs();
        let scan_range = range(a[axis].min(line.b[axis])-scan_reach,a[axis].max(line.b[axis])+scan_reach,upper[axis]);
        let perpendicular_reach = scan_reach/(d[axis].abs()*inv_length);
        let inv_d = d[axis].recip();
        let inv_len_sq = length_sq.recip();
        let hidden = style.flags & HIDDEN != 0;
        let round_cuts = line.outside == 0.
            && style.flags & (OUTLINE | BORDER | MARK) == 0
            && style.flags & (CREASE | MATERIAL) != 0
            && style.wave[0] == 0. && style.dash[1] == 0.
            && stamp.is_none() && span.is_none() && world.is_none()
            && length <= 4. * self.width.max(self.height) as f32;
        let mut cut_runs = None;
        for i in scan_range.start.max(lower[axis])..scan_range.end {
            let scan = i as f32+0.5-a[axis];
            let middle = a[cross]+(scan*inv_d).clamp(0.0,1.0)*d[cross];
            let cross_span = range(middle-perpendicular_reach,middle+perpendicular_reach,upper[cross]);
            for j in cross_span.start.max(lower[cross])..cross_span.end {
                let other = j as f32+0.5-a[cross];
                let projected = (scan*d[axis]+other*d[cross])*inv_len_sq;
                let t = projected.clamp(0.0,1.0);
                let dx = scan-t*d[axis];
                let dy = other-t*d[cross];
                let mut distance_sq = dx*dx+dy*dy;
                let (x,y) = if axis == 0 {(i,j)} else {(j,i)};
                let mut across = 0.;
                if stamp.is_some() || style.wave[0] != 0. || style.dash[1] > 0. {
                    across = (-d[1]*(x as f32+0.5-a[0])+d[0]*(y as f32+0.5-a[1]))*inv_length;
                }
                if let Some(span) = span {
                    if style.wave[0] != 0. {
                        let side = if line.outside != 0. { line.outside }
                            else if span.start <= span.end { 1. } else { -1. };
                        let position = projected * length;
                        if let Some(runs) = wave_runs {
                            distance_sq = reach * reach;
                            let start = runs.partition_point(|run| run.bounds[1] < position-reach);
                            let original_cross = across;
                            for run in &runs[start..] {
                                if run.bounds[0] > position+reach { break; }
                                let distance = wave_distance(position, original_cross, length, reach,
                                    span, style.wave, side, run.bounds, run.phase_origin);
                                if distance.0 < distance_sq {
                                    (distance_sq, across) = distance;
                                }
                            }
                        } else {
                            let side = if span.directed_wave_side != 0. { span.directed_wave_side } else { side };
                            (distance_sq, across) = wave_distance(position, across, length, reach,
                                span, style.wave, side, [0., length], 0.);
                        }
                    }
                    let [dash, gap, phase] = style.dash;
                    if gap > 0. {
                        let along = span.start + t*(span.end-span.start);
                        let cycle = (along + phase).rem_euclid(dash + gap);
                        if cycle > dash {
                            let overrun = (projected-t).abs()*length;
                            let offset = (cycle-dash).min(dash+gap-cycle).max(overrun);
                            distance_sq = across*across + offset*offset;
                        }
                    }
                }
                if distance_sq >= reach*reach { continue; }
                let center = [a[0]+t*d[0],a[1]+t*d[1],a[2]+t*d[2]];
                if wave_runs.is_none() && self.visible(center, outward, line.owner) == hidden {
                    if !round_cuts { continue; }
                    let runs = cut_runs.get_or_insert_with(||
                        self.depth_runs(line, d, outward, length, hidden));
                    let position = projected * length;
                    let start = runs.partition_point(|run| run[1] < position-reach);
                    let mut closest = reach * reach;
                    for &[lo, hi] in &runs[start..] {
                        if lo > position+reach { break; }
                        let end = position.clamp(lo, hi)/length;
                        closest = closest.min((scan-end*d[axis]).powi(2)
                                              + (other-end*d[cross]).powi(2));
                    }
                    distance_sq = closest;
                }
                // An Outline is an exterior contour, not every front/back
                // transition inside the same mesh (for example a dish rim).
                // Keep independent open borders and crease/marked strokes.
                if line.exterior_only {
                    let (backing, owner) = self.sample(
                        (center[0]+1.5*outward[0]).floor() as i32,
                        (center[1]+1.5*outward[1]).floor() as i32);
                    if interior_backing(line.owner, backing, owner) {
                        continue;
                    }
                }
                let mut radius = style.radius;
                let mut opacity = style.rgba[3];
                if let Some([wa, wb]) = world {
                    let u = t * wb[3] / ((1. - t) * wa[3] + t * wb[3]).max(1e-20);
                    let point = std::array::from_fn(|i| wa[i] + (wb[i] - wa[i]) * u);
                    if let Some(size) = &reduction[0] { radius *= size.factor(point); }
                    if let Some(alpha) = &reduction[1] { opacity *= alpha.factor(point); }
                }
                if let Some(span) = span {
                    let along = span.start + t * (span.end - span.start);
                    let parameter = along / span.total.max(1e-5);
                    if let Some(size) = &profiles[0] { radius *= sample_curve(size, parameter); }
                    if let Some(alpha) = &profiles[1] { opacity *= sample_curve(alpha, parameter); }
                }
                if radius <= 0. || opacity <= 0. || distance_sq >= (radius + aa*0.5).powi(2) {
                    continue;
                }
                let coverage = if aa == 0.0 { 1.0 }
                    else { (0.5+(radius-distance_sq.sqrt())/aa).clamp(0.0,1.0) };
                let mut alpha = coverage*opacity;
                if let (Some(stamp), Some(span)) = (stamp, span) {
                    if (projected<0.0 && !span.caps[0]) || (projected>=1.0 && !span.caps[1]) { continue; }
                    let side = if span.start <= span.end {1.0} else {-1.0};
                    alpha *= stamp.sample(span.start+projected*(span.end-span.start), across*side, 2.0*radius);
                    if alpha <= 0.0 { continue; }
                }
                let pixel = &mut output[(y-bottom)*width+x-left];
                for (c, &value) in pixel[..3].iter_mut().zip(&style.rgba[..3]) {
                    *c = value*alpha + *c*(1.0-alpha);
                }
                pixel[3] = alpha + pixel[3]*(1.0-alpha);
            }
        }
    }

    fn depth_runs(&self, line: Stroke, delta: Vec3, outward: [f32; 2],
                  length: f32, hidden: bool) -> Vec<[f32; 2]> {
        let visible = |t: f32| {
            self.visible(std::array::from_fn(|i| line.a[i] + delta[i]*t), outward, line.owner)
                != hidden
        };
        let steps = (2. * length).ceil().max(1.) as usize;
        let mut runs = Vec::new();
        let mut previous = visible(0.);
        let mut start = 0.;
        for i in 1..=steps {
            let mut high = i as f32 / steps as f32;
            let current = visible(high);
            if current == previous { continue; }
            let mut low = (i-1) as f32 / steps as f32;
            for _ in 0..10 {
                let mid = (low+high)*0.5;
                if visible(mid) == previous { low = mid; } else { high = mid; }
            }
            let cut = (low+high)*0.5*length;
            if previous { runs.push([start, cut]); } else { start = cut; }
            previous = current;
        }
        if previous { runs.push([start, length]); }
        runs
    }

    fn chain_wave_runs(&self, ink: &[Stroke], spans: &[StampSpan],
                       allow_shallow_turns: bool, hidden: bool) -> Vec<Option<Vec<WaveRun>>> {
        let mut result: Vec<Option<Vec<WaveRun>>> = (0..ink.len()).map(|_| None).collect();
        for (head, first) in spans.iter().enumerate() {
            let head_side = usize::from(first.start > first.end);
            if first.start.min(first.end) != 0. || !first.caps[head_side]
                || first.total > 4. * self.width.max(self.height) as f32 { continue; }
            let direction = |index: usize| {
                let d = minus(ink[index].b, ink[index].a);
                let length = d[0].hypot(d[1]);
                let sign = if spans[index].start <= spans[index].end { 1. } else { -1. };
                [d[0]*sign/length, d[1]*sign/length]
            };
            let tangent = direction(head);
            let mut cursor = Some(head);
            let mut chain = Vec::new();
            while let Some(index) = cursor {
                let axis = direction(index);
                // Restrict the phase transfer to shallow turns whose
                // displaced stroke remains continuous at the shared vertex.
                let turns_too_far = if allow_shallow_turns {
                    axis[0]*tangent[0] + axis[1]*tangent[1] < 0.85
                } else {
                    (axis[0]-tangent[0]).abs() > 1e-4 || (axis[1]-tangent[1]).abs() > 1e-4
                };
                if turns_too_far {
                    chain.clear();
                    break;
                }
                chain.push(index);
                cursor = spans[index].next;
            }
            let mut phase_origin = 0.;
            let mut continues = false;
            let mut has_cut = false;
            for &index in &chain {
                let span = &spans[index];
                let line = ink[index];
                let delta = minus(line.b, line.a);
                let length = delta[0].hypot(delta[1]);
                let outward = [-delta[1]*line.outside/length, delta[0]*line.outside/length];
                let mut runs = self.depth_runs(line, delta, outward, length, hidden);
                has_cut |= runs.as_slice() != [[0., length]];
                let forward = span.start <= span.end;
                if !forward { runs.reverse(); }
                let mut resolved = Vec::new();
                for bounds in runs {
                    let (entry, exit) = if forward { (bounds[0], bounds[1]) }
                                        else { (length-bounds[1], length-bounds[0]) };
                    if !continues || entry > 0. {
                        phase_origin = span.start.min(span.end) + entry;
                    }
                    resolved.push(WaveRun { bounds, phase_origin });
                    continues = exit == length;
                }
                if resolved.is_empty() { continues = false; }
                if !forward { resolved.reverse(); }
                result[index] = Some(resolved);
            }
            if !has_cut {
                for index in chain { result[index] = None; }
            }
        }
        result
    }
}

fn wave_distance(x: f32, y: f32, length: f32, reach: f32, span: &StampSpan,
                 wave: Vec3, side: f32, run: [f32; 2], origin: f32) -> (f32, f32) {
    let [amplitude, wavelength, phase] = wave;
    let low = (x-reach).max(run[0]);
    let high = (x+reach).min(run[1]);
    if high < low || y.abs() > amplitude.abs()+reach { return (reach*reach, y); }
    let height = |at: f32| {
        let along = span.start - origin + at/length*(span.end-span.start);
        side * amplitude * (std::f32::consts::TAU*along/wavelength + phase).sin()
    };
    let steps = ((high-low) / (wavelength/8.).min(1.)).ceil().clamp(1., 64.) as usize;
    let (mut ax, mut ay) = (low, height(low));
    let (mut best, mut cross) = (reach*reach, y);
    for step in 1..=steps {
        let bx = low + (high-low)*step as f32/steps as f32;
        let by = height(bx);
        let (dx, dy) = (bx-ax, by-ay);
        let span_sq = dx*dx+dy*dy;
        let t = (((x-ax)*dx+(y-ay)*dy)/span_sq.max(1e-12)).clamp(0., 1.);
        let (sx, sy) = (x-ax-t*dx, y-ay-t*dy);
        let distance = sx*sx+sy*sy;
        if distance < best {
            best = distance;
            cross = if span_sq > 1e-12 { (sy*dx-sx*dy)/span_sq.sqrt() } else { sy };
        }
        (ax, ay) = (bx, by);
    }
    (best, cross)
}

fn interior_backing(owner: u32, backing: f32, backing_owner: u32) -> bool {
    // Outline describes the object's projected exterior. A same-object hit
    // on its outward side remains interior even at a shallow/tangent fold.
    backing_owner == owner && backing < 1.0
}

#[cfg(test)]
mod outline_tests {
    use super::*;

    #[test]
    fn owner_corrected_depth_is_cubic_even_inside_one_cell() {
        let depth = (0..16).map(|i| {
            let owner=if i==0 {1u32} else {2u32};
            AtomicU64::new(((0.7f32.to_bits() as u64)<<32)|owner as u64)
        }).collect();
        let frame=Frame {width:4,height:4,styles:vec![],stamps:vec![],reductions:vec![],stroke_curves:vec![],depth};
        let samples=|outward| {
            [0.,1./3.,2./3.,1.].map(|t| {
                let point=[0.6+0.3*t,0.6+0.3*t,0.4+0.2*t];
                frame.visibility_limit(point,outward,1)-point[2]
            })
        };
        let third=|f:[f32;4]| f[3]-3.*f[2]+3.*f[1]-f[0];
        assert!(third(samples([0.,0.])).abs()<1e-6);
        // One inward tap follows line z(t), multiplying its quadratic weight.
        assert!(third(samples([1.,0.])).abs()>0.001);
    }

    #[test]
    fn same_mesh_backing_rejects_interior_at_shallow_folds() {
        assert!(interior_backing(7, 0.6, 7));
        assert!(!interior_backing(7, 0.6, 8));
        assert!(interior_backing(7, 0.5, 7));
        assert!(interior_backing(7, 0.4, 7));
        assert!(!interior_backing(7, 1.0, 0));
    }
}

#[derive(Clone, Copy)]
struct Incident {
    face: usize,
    forward: bool,
}

#[derive(Clone, Copy, Default)]
struct EdgeFaces {
    first: u32,
    second: u32,
    extra: u32,
}

fn decode(value: u32) -> Incident {
    Incident { face: ((value >> 1)-1) as usize, forward: value&1 != 0 }
}

unsafe fn prepare(frame: &Frame, mesh: &Mesh) -> Option<Ink> {
    let vertices = slice::from_raw_parts(mesh.vertices,mesh.vertex_count);
    let triangles = slice::from_raw_parts(mesh.triangles,mesh.triangle_count);
    let jobs = slice::from_raw_parts(mesh.style_ids,mesh.style_count);
    let face_masks: Vec<_> = if mesh.face_masks.is_null() {
        Vec::new()
    } else {
        slice::from_raw_parts(mesh.face_masks, jobs.len()).iter().map(|&mask| {
            (!mask.is_null()).then(|| slice::from_raw_parts(mask, mesh.face_count))
        }).collect()
    };
    let size = [frame.width as f32,frame.height as f32];
    let mut clips = Vec::with_capacity(vertices.len());
    let mut projected = Vec::with_capacity(vertices.len());
    let mut codes = Vec::with_capacity(vertices.len());
    for &[x,y,z] in vertices {
        let p = mesh.projection.map(|r| r[0]*x+r[1]*y+r[2]*z+r[3]);
        clips.push(p);
        codes.push(outcode(p));
        projected.push(screen(p,size));
    }
    for &[a,b,c] in triangles {
        let [a,b,c] = [a as usize,b as usize,c as usize];
        if codes[a]&codes[b]&codes[c] != 0 { continue; }
        if (codes[a]|codes[b]|codes[c])&48 == 0 {
            frame.triangle(projected[a],projected[b],projected[c],mesh.owner);
        } else {
            frame.clipped_triangle([clips[a],clips[b],clips[c]],size,mesh.owner);
        }
    }
    let mut ink = Ink {strokes:vec![Vec::new();frame.styles.len()],
                       stamp_spans:vec![Vec::new();frame.styles.len()],
                       reduction_points:vec![Vec::new();frame.styles.len()]};
    if jobs.is_empty() { return Some(ink); }
    let edges = slice::from_raw_parts(mesh.edges,mesh.edge_count);
    let marks = (!mesh.marks.is_null()).then(|| slice::from_raw_parts(mesh.marks,mesh.edge_count));
    let materials = (!mesh.face_materials.is_null())
        .then(|| slice::from_raw_parts(mesh.face_materials, mesh.face_count));
    let mut stamp_edges = vec![Vec::new(); frame.styles.len()];
    let loop_edges = slice::from_raw_parts(mesh.loop_edges,mesh.loop_count);
    let loop_vertices = slice::from_raw_parts(mesh.loop_vertices,mesh.loop_count);
    let sizes = slice::from_raw_parts(mesh.sizes,mesh.face_count);
    let normals = slice::from_raw_parts(mesh.normals,mesh.face_count);
    let mut adjacency = vec![EdgeFaces::default();edges.len()];
    let mut extra = Vec::<(u32,u32)>::new();
    let mut offset = 0usize;
    let mut directions = Vec::with_capacity(sizes.len());
    let mut front = Vec::with_capacity(sizes.len());
    for (index, (&count,&normal)) in sizes.iter().zip(normals).enumerate() {
        let end = offset.checked_add(count as usize)?;
        if count == 0 { return None; }
        let corners = loop_edges.get(offset..end)?;
        let corner_vertices = loop_vertices.get(offset..end)?;
        let origin = vertices[corner_vertices[0] as usize];
        let eye = if mesh.perspective != 0 { minus(mesh.eye,origin) } else { mesh.eye };
        front.push(dot(eye,normal) >= 1e-7);
        let world = mesh.normal.map(|row| dot(row,normal));
        let length = dot(world,world).sqrt().max(1e-20);
        directions.push(world.map(|value| value/length));
        for (&edge,&vertex) in corners.iter().zip(corner_vertices) {
            let i = edge as usize;
            let value = ((index as u32+1)<<1) | (edges[i][0] == vertex) as u32;
            let entry = &mut adjacency[i];
            if entry.first == 0 { entry.first = value; }
            else if entry.second == 0 { entry.second = value; }
            else {
                extra.push((value,entry.extra));
                entry.extra = extra.len() as u32;
            }
        }
        offset = end;
    }
    if offset != loop_edges.len() { return None; }

    for (index, (&[a,b], neighbors)) in edges.iter().zip(adjacency).enumerate() {
        let [a,b] = [a as usize,b as usize];
        let outside_frame = codes[a]&codes[b];
        if neighbors.first == 0 || outside_frame&48 != 0 { continue; }
        let first = decode(neighbors.first);
        let second = (neighbors.second != 0).then(|| decode(neighbors.second));
        let first_front = front[first.face];
        let mut front_face = first_front.then_some(first);
        let mut back_face = !first_front;
        if let Some(face) = second {
            if front[face.face] { front_face.get_or_insert(face); }
            else { back_face = true; }
        }
        let mut extras = neighbors.extra;
        while extras != 0 {
            let (value,next) = extra[extras as usize-1];
            let face = decode(value);
            if front[face.face] { front_face.get_or_insert(face); }
            else { back_face = true; }
            extras = next;
        }
        let silhouette = front_face.is_some() && back_face;
        let marked = second.is_some() && marks.is_some_and(|values| values[index]!=0);
        for (slot, &job) in jobs.iter().enumerate() {
            let style = frame.styles[job as usize];
            let selected = face_masks.get(slot).copied().flatten();
            let chained = frame.stamps[job as usize].is_some()
                || style.wave[0] != 0. || style.dash[1] > 0.
                || frame.stroke_curves[job as usize].iter().any(Option::is_some);
            // Offscreen strokes can still contribute to a texture's phase.
            if outside_frame != 0 && !chained { continue; }
            let contour = silhouette && style.flags&OUTLINE != 0;
            let boundary = second.is_none() && style.flags&BORDER != 0;
            let material_edge = style.flags & (MATERIAL | EXCLUDE_MATERIAL) != 0
                && second.is_some_and(|other| {
                    materials.is_some_and(|ids| {
                        any_face_pair(first, other, neighbors.extra, &extra, selected,
                                      |a, b| ids[a] != ids[b])
                    })
                });
            let material = material_edge && style.flags&MATERIAL != 0
                && !(silhouette && style.flags&RESERVE_OUTLINE != 0);
            if material_edge && style.flags&EXCLUDE_MATERIAL != 0 { continue; }
            let mark = marked && style.flags&MARK != 0
                && !(silhouette && style.flags&RESERVE_OUTLINE != 0);
            let crease = second.is_some() && style.flags&CREASE != 0
                && !(marked && style.flags&EXCLUDE_MARK != 0)
                && !(silhouette && style.flags&RESERVE_OUTLINE != 0)
                && has_crease(first, second.unwrap(), neighbors.extra, &extra, &directions,
                              style, selected);
            if !(contour || boundary || crease || mark || material) { continue; }
            let outside = if (contour || boundary) && front_face.is_some() {
                if front_face.unwrap().forward {-mesh.handedness} else {mesh.handedness}
            } else { 0.0 };
            let (mut p,mut q) = (projected[a],projected[b]);
            let variable = frame.reductions[job as usize].iter().any(Option::is_some);
            let mut world = [[0.; 4]; 2];
            if variable {
                for (end, vertex) in [a,b].into_iter().enumerate() {
                    for i in 0..3 {
                        let row = mesh.world[i];
                        world[end][i] = dot([row[0],row[1],row[2]], vertices[vertex]) + row[3];
                    }
                }
            }
            let (mut left,mut right) = (clips[a],clips[b]);
            if (codes[a]|codes[b])&48 != 0 {
                for sign in [1.0,-1.0] {
                    let d0 = left[3]+left[2]*sign;
                    let d1 = right[3]+right[2]*sign;
                    if d0 < 0.0 {
                        let t = d0/(d0-d1);
                        left = lerp(left,right,t);
                        if variable { world[0] = lerp(world[0],world[1],t); }
                    } else if d1 < 0.0 {
                        let t = d1/(d1-d0);
                        right = lerp(right,left,t);
                        if variable { world[1] = lerp(world[1],world[0],t); }
                    }
                }
                p = screen(left,size);
                q = screen(right,size);
            }
            if chained && (p[0]-q[0]).hypot(p[1]-q[1]) < 1e-5 { continue; }
            if variable {
                world[0][3] = left[3].recip();
                world[1][3] = right[3].recip();
                ink.reduction_points[job as usize].push(world);
            }
            let directed_mark = style.wave[0] != 0.
                && (mesh.perspective != 0 || style.flags & WAVE_DIRECTION != 0)
                && mark && style.flags & (OUTLINE | BORDER | CREASE | MATERIAL | HIDDEN) == 0
                && neighbors.extra == 0 && second.is_some_and(|other| {
                    let delta = minus(directions[first.face], directions[other.face]);
                    dot(delta, delta) <= 1e-12
                });
            let directed_contour = style.flags & WAVE_DIRECTION != 0 && (contour || crease);
            let directed_wave_side = if style.wave[0] != 0. && (directed_mark || directed_contour) {
                let face = if directed_contour {
                    let eye = if mesh.perspective != 0 { minus(mesh.eye, vertices[a]) } else { mesh.eye };
                    let world_eye = mesh.world.map(|row| dot([row[0], row[1], row[2]], eye));
                    second.filter(|other| {
                        let difference = minus(directions[other.face], directions[first.face]);
                        dot(difference, world_eye) > 1e-6 * dot(world_eye, world_eye).sqrt()
                    }).unwrap_or(first)
                } else { first };
                if face.forward == directed_mark { mesh.handedness } else { -mesh.handedness }
            } else { 0. };
            ink.strokes[job as usize].push(Stroke {a:p,b:q,outside,directed_wave_side,owner:mesh.owner,
                exterior_only:contour && !boundary && !crease && !mark && !material});
            if chained {
                stamp_edges[job as usize].push([
                    if codes[a]&48==0 {a as u32} else {CUT_VERTEX},
                    if codes[b]&48==0 {b as u32} else {CUT_VERTEX},
                ]);
            }
        }
    }
    for &job in jobs {
        let i = job as usize;
        if !stamp_edges[i].is_empty() {
            let style = frame.styles[i];
            let angle = if style.flags & WAVE_DIRECTION != 0 { Some(style.dash[2]) }
                        else { None };
            ink.stamp_spans[i] = stamp_spans(&ink.strokes[i], &stamp_edges[i], mesh.vertex_count, angle,
                style.flags & WAVE_COUNTERCLOCKWISE != 0, (style.flags >> 11) & 7);
        }
    }
    Some(ink)
}

fn has_crease(first: Incident, second: Incident, extra_start: u32, extra: &[(u32,u32)],
              normals: &[Vec3], style: Style, selected: Option<&[u8]>) -> bool {
    any_face_pair(first, second, extra_start, extra, selected, |a, b| {
        let cosine = dot(normals[a], normals[b]);
        cosine >= style.min_cos && cosine <= style.max_cos
    })
}

fn any_face_pair(first: Incident, second: Incident, extra_start: u32, extra: &[(u32,u32)],
                 selected: Option<&[u8]>, predicate: impl Fn(usize, usize) -> bool) -> bool {
    let check = |a: Incident,b: Incident| {
        if selected.is_some_and(|mask| mask[a.face] == 0 && mask[b.face] == 0) {
            return false;
        }
        predicate(a.face, b.face)
    };
    if check(first,second) { return true; }
    let mut index = extra_start;
    while index != 0 {
        let (value,next) = extra[index as usize-1];
        let face = decode(value);
        if check(first,face) || check(second,face) { return true; }
        let mut remaining = next;
        while remaining != 0 {
            let (other,after) = extra[remaining as usize-1];
            if check(face,decode(other)) { return true; }
            remaining = after;
        }
        index = next;
    }
    false
}

#[no_mangle]
pub extern "C" fn rea_cpu_lines_abi_version() -> u32 { 7 }

#[no_mangle]
pub unsafe extern "C" fn rea_cpu_lines_create(
    width: usize,height: usize,styles: *const StyleInput,count: usize,
) -> *mut Frame {
    catch_unwind(|| {
        let Some(pixels) = width.checked_mul(height) else { return ptr::null_mut(); };
        if pixels == 0 || pixels > isize::MAX as usize/8 { return ptr::null_mut(); }
        let mut depth = Vec::new();
        if depth.try_reserve_exact(pixels).is_err() { return ptr::null_mut(); }
        depth.resize_with(pixels,|| AtomicU64::new(EMPTY));
        let mut resolved = Vec::with_capacity(count);
        let mut stamps = Vec::with_capacity(count);
        let mut reductions = Vec::with_capacity(count);
        let mut stroke_curves = Vec::with_capacity(count);
        for style in slice::from_raw_parts(styles,count) {
            let stamp = if style.stamp.is_null() { None } else {
                let Some(size) = style.stamp_width.checked_mul(style.stamp_height) else { return ptr::null_mut(); };
                if size==0 || size>2048*64 || !style.stamp_period.is_finite() || style.stamp_period<=0.0 {
                    return ptr::null_mut();
                }
                Some(Stamp {coverage:slice::from_raw_parts(style.stamp,size).to_vec(),
                            width:style.stamp_width,height:style.stamp_height,period:style.stamp_period})
            };
            let profiles = style.stroke_curves.map(|curve| (!curve.is_null())
                .then(|| slice::from_raw_parts(curve,4097).to_vec()));
            let max_stroke_size = profiles[0].as_ref()
                .map_or(1., |curve| curve.iter().copied().fold(1., f32::max));
            stroke_curves.push(profiles);
            resolved.push(Style {flags:style.flags,min_cos:style.min_cos,max_cos:style.max_cos,
                                 radius:style.radius,aa:style.aa,rgba:style.rgba,max_stroke_size,
                                 wave:style.wave,dash:style.dash});
            stamps.push(stamp);
            reductions.push(std::array::from_fn(|index| {
                let input = &style.reduction[index];
                (input.enabled != 0).then(|| Reduction {
                    range_factor: input.range_factor,
                    reference: input.reference,
                    curve: (!input.curve.is_null())
                        .then(|| slice::from_raw_parts(input.curve,4097).to_vec()),
                })
            }));
        }
        Box::into_raw(Box::new(Frame {width,height,styles:resolved,stamps,reductions,stroke_curves,depth}))
    }).unwrap_or(ptr::null_mut())
}

#[no_mangle]
pub unsafe extern "C" fn rea_cpu_lines_prepare(frame: *const Frame, mesh: *const Mesh) -> *mut Ink {
    catch_unwind(|| match prepare(&*frame,&*mesh) {
        Some(value) => Box::into_raw(Box::new(value)),
        None => ptr::null_mut(),
    }).unwrap_or(ptr::null_mut())
}

#[no_mangle]
pub unsafe extern "C" fn rea_cpu_lines_draw(
    frame: *const Frame, parts: *const *const Ink,count: usize,rect: *const [usize;4],
    output: *mut [f32;4],
) -> usize {
    catch_unwind(|| {
        let frame = &*frame;
        let parts = slice::from_raw_parts(parts,count);
        let rect = *rect;
        let [x0,y0,x1,y1] = rect;
        if x0 >= x1 || y0 >= y1 || x1 > frame.width || y1 > frame.height { return usize::MAX; }
        let pixels = slice::from_raw_parts_mut(output,(x1-x0)*(y1-y0));
        pixels.fill([0.0;4]);
        let mut strokes = 0;
        for index in 0..frame.styles.len() {
            for &part in parts {
                let part = &*part;
                let ink = &part.strokes[index];
                strokes += ink.len();
                let points = &part.reduction_points[index];
                let spans = &part.stamp_spans[index];
                let style = frame.styles[index];
                let hidden = style.flags & HIDDEN != 0;
                let categories = style.flags & (OUTLINE | BORDER | CREASE | MATERIAL | MARK);
                let wave_runs = if style.wave[0] != 0. && (categories == MARK || hidden && categories == CREASE) {
                    let allow_shallow_turns = style.wave[0].abs() <= 2.*style.radius
                        && frame.reductions[index][0].is_none();
                    frame.chain_wave_runs(ink, spans, allow_shallow_turns, hidden)
                } else {
                    Vec::new()
                };
                for (i, &stroke) in ink.iter().enumerate() {
                    frame.stroke(stroke,index,rect,pixels,spans.get(i),points.get(i),
                                 wave_runs.get(i).and_then(Option::as_deref));
                }
            }
        }
        strokes
    }).unwrap_or(usize::MAX)
}

#[no_mangle]
pub unsafe extern "C" fn rea_cpu_lines_free(frame: *mut Frame) {
    if !frame.is_null() { drop(Box::from_raw(frame)); }
}

#[no_mangle]
pub unsafe extern "C" fn rea_cpu_lines_free_mesh(mesh: *mut Ink) {
    if !mesh.is_null() { drop(Box::from_raw(mesh)); }
}
