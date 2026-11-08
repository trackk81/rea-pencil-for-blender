"""Independent conversion of nominal brush pixels to stroke coverage pixels.

PHYSICAL preserves the existing Line Art width. PENCIL_SIMPLE is an opt-in
empirical approximation measured from the installed original SIMPLE brush:
axis-aligned, diagonal and curved edges at 512px; widths .5 through 8; AA 1.
It approximates integrated alpha coverage, not the private raster algorithm,
edge classification, or exact antialiasing. Widths outside the measured range
extrapolate the same continuous model and have not been validated.

Evidence: artifacts/original_aa_v05/report*.json and probe_original_aa_v05.py.
"""
import math


def live_coverage_supported(brush):
    """Bound the live calibration to constant-width/opacity solid strokes."""
    return not any(getattr(brush, name, False) for name in (
        'dashed', 'taper', 'size_curve_enabled', 'alpha_curve_enabled',
        'distance_fade', 'distance_alpha'))


def live_variable_coverage_supported(brush):
    return getattr(brush,'line_direction_enabled',False) and (getattr(brush,'size_curve_enabled',False) or getattr(brush,'taper',False)) and not any(
        getattr(brush,name,False) for name in ('dashed','alpha_curve_enabled','distance_fade','distance_alpha'))


def live_variable_coverage_factor(width):
    """Conservative open-curve gain; full base gain over-darkened variable ink."""
    return 1 + .25*(live_coverage_width(width)/width-1) if width>0 else 1


def live_coverage_width(width):
    """Opt-in base coverage: retain subpixel widths where trials regressed.

    Measured for solid Simple-like outlines1..6px, opacity.25/.5/1, two phases.
    This modifies coverage width, not the native raster antialiasing kernel.
    Above the measured range the continuous model is an unverified extrapolation.
    """
    width = coverage_width(width, 'PHYSICAL')
    return coverage_width(width, 'PENCIL_SIMPLE') if width >= 1 else width


def coverage_width(width, profile='PHYSICAL'):
    width = float(width)
    if not math.isfinite(width) or width < 0:
        raise ValueError('Stroke width must be finite and nonnegative')
    if profile == 'PHYSICAL':
        return width
    if profile != 'PENCIL_SIMPLE':
        raise ValueError('Unknown stroke width profile: ' + str(profile))
    if width < 1:
        return width * width
    if width < 2:
        return 2 * width - 1
    return width + 1
