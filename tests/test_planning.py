"""Tests for region math and --only spec parsing."""

from __future__ import annotations

import itertools

import pytest

from drivetest.planning import (
    Region,
    parse_only_spec,
    plan_regions,
    quick_region,
)
from drivetest.units import GIB, MIB


def test_regions_tile_the_device_exactly():
    dev = 2000398934016  # 2 TB, not MiB-aligned
    regions = plan_regions(dev, 8)
    assert len(regions) == 8
    # contiguous, no gaps or overlaps
    assert regions[0].offset == 0
    for prev, nxt in itertools.pairwise(regions):
        assert nxt.offset == prev.end
    # last region covers the remainder up to the exact device size
    assert regions[-1].end == dev


def test_regions_are_mib_aligned_except_final_remainder():
    dev = 2000398934016
    regions = plan_regions(dev, 8)
    for region in regions[:-1]:
        assert region.offset % MIB == 0
        assert region.size % MIB == 0
    # the final region's size absorbs the sub-MiB remainder
    assert regions[-1].offset % MIB == 0
    assert regions[-1].size % MIB != 0  # it actually holds the sub-MiB tail
    assert regions[-1].end == dev  # ...up to the exact device size


def test_single_part_covers_whole_device():
    regions = plan_regions(1000, 1)
    assert regions == [Region(index=1, offset=0, size=1000)]


def test_part_size_matches_shell_arithmetic():
    # dev // parts // MIB * MIB, then last part gets the rest
    dev = 100 * MIB + 12345
    regions = plan_regions(dev, 4)
    expected_part = dev // 4 // MIB * MIB
    assert regions[0].size == expected_part
    assert regions[-1].size == dev - 3 * expected_part


def test_plan_regions_rejects_bad_input():
    with pytest.raises(ValueError):
        plan_regions(1000, 0)
    with pytest.raises(ValueError):
        plan_regions(0, 4)
    with pytest.raises(ValueError, match="too small"):
        plan_regions(MIB - 1, 4)  # each part would round to 0 bytes


def test_quick_region_is_a_leading_span():
    region = quick_region(50 * GIB)
    assert region.index == 1
    assert region.offset == 0
    assert region.size == 50 * GIB


@pytest.mark.parametrize(
    ("spec", "parts", "expected"),
    [
        ("5", 8, {5}),
        ("1-4", 8, {1, 2, 3, 4}),
        ("5-8", 8, {5, 6, 7, 8}),
        ("1-3,7", 8, {1, 2, 3, 7}),
        ("6-", 8, {6, 7, 8}),
        ("1-", 3, {1, 2, 3}),
        (" 2 , 4 ", 8, {2, 4}),  # whitespace tolerated
        ("3-3", 8, {3}),
        ("1,1", 8, {1}),  # duplicate collapses
        ("1-3,2-4", 8, {1, 2, 3, 4}),  # overlapping ranges union
        ("1 - 4", 8, {1, 2, 3, 4}),  # whitespace inside a range
    ],
)
def test_parse_only_spec_valid(spec, parts, expected):
    assert parse_only_spec(spec, parts) == expected


@pytest.mark.parametrize(
    ("spec", "parts"),
    [
        ("0", 8),       # below range
        ("9", 8),       # above range
        ("5-9", 8),     # end above range
        ("4-2", 8),     # inverted range
        ("abc", 8),     # not a number
        ("1-b", 8),     # bad tail
        ("1,,2", 8),    # empty item
        ("", 8),        # empty spec
        ("-3", 8),      # missing head
        ("9-", 8),      # open range with out-of-range head
        ("1-2-3", 8),   # multiple dashes
        ("²", 8),       # non-ASCII digit (isdigit true, int would reject)
    ],
)
def test_parse_only_spec_invalid(spec, parts):
    with pytest.raises(ValueError):
        parse_only_spec(spec, parts)


def test_parse_only_spec_rejects_bad_parts():
    with pytest.raises(ValueError):
        parse_only_spec("1", 0)


def test_region_indices_are_sequential():
    regions = plan_regions(100 * MIB, 5)
    assert [r.index for r in regions] == [1, 2, 3, 4, 5]
