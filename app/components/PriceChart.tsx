'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { PriceData } from '@/lib/prices';
import type { DispatchSlot } from '@/lib/optimizer';
import {
  buildAreaPath,
  buildChartGeometry,
  buildLinePath,
  buildStepLinePath,
  computeNowSlotTime,
  pinchZoomRange,
  priceYScale,
  socYScale,
  solarYScale,
  sumActualForBand,
} from '@/lib/chart-utils';
import type { ActualSlotFlows } from '@/lib/actual-flows';
import { useChartData } from './useChartData';
import { buildFrame } from './price-chart/frame';
import { useContainerWidth, useMobileViewport } from './useResponsiveLayout';
import { mobileChartWindow } from '@/lib/mobile-chart';
import { DayZones, DecisionBands, MinorChargeBands } from './price-chart/ChartBands';
import { PriceAxis, SocAxis, TimeAxis } from './price-chart/ChartAxes';
import ChartLegend from './price-chart/ChartLegend';
import type { ChartLayers } from './price-chart/ChartLegend';
import ChartTooltip from './price-chart/ChartTooltip';

/**
 * The Elpriser card: today's (and tomorrow's) prices with the optimizer's plan drawn over them.
 *
 * Hand-rolled SVG rather than a charting library (Recharts was removed early on — the chart
 * needs enough bespoke drawing that the library was mostly being fought).
 * This file owns the DATA WIRING and COMPOSITION only; each visual layer lives in
 * ./price-chart/ and is drawn here in z-order, back to front:
 *
 *   DayZones     today/tomorrow tint          ChartBands.tsx
 *   DecisionBands  optimizer decision zones     "
 *   PriceAxis    gridlines + öre/kWh labels   ChartAxes.tsx
 *   SocAxis      right-hand 0-100% scale        "
 *   (solar area, SoC lines, price lines)      below — short enough to read inline
 *   (now marker, hover guide)                 below
 *   TimeAxis     hour labels                  ChartAxes.tsx
 *   ChartTooltip hover detail (HTML, not SVG) ChartTooltip.tsx
 *
 * Split out of one ~600-line function on 2026-08-07. The series and overlay stay inline
 * deliberately: they are a handful of <path> elements each, and pushing them out too would
 * leave this file a list of imports that says nothing about what the chart looks like.
 */

interface Props {
  data: PriceData;
  solarProfiles: Record<number, number[]>;
  solarForecast?: Record<string, number[]> | null;
  dispatchSchedule?: DispatchSlot[] | null;
  pastDispatchSlots?: DispatchSlot[] | null; // reconstructed historical decision bands (see lib/telemetry readPastDispatchSlots)
  pastPlannedSocKwhByTime?: Record<string, number>; // historical plan SoC at slot boundaries
  startSocKwh?: number; // SoC the optimizer planned from
  socIsLive?: boolean; // true = live inverter reading, false = 50% fallback
  actualSocByTime?: Record<string, number>; // real measured SoC %, keyed "YYYY-MM-DDTHH:MM"
  actualFlowsByTime?: Record<string, ActualSlotFlows>; // measured battery flows, keyed by slot startTime
  interventionsByTime?: Record<string, string[]>; // control_actions outcomes (non-'applied'), keyed by slot startTime
  // Resolved server-side (see app/page.tsx) and passed in rather than imported from
  // lib/constants directly: this component is 'use client', and Next.js never exposes
  // non-NEXT_PUBLIC_ env vars to the client bundle — a direct import would silently read the
  // hardcoded fallback instead of the deployment's real env-configured value.
  batteryKwh: number;
  skattOverforing: number;
  batteryFloorKwh: number;
}

type Point = [number, number];

// Groups a series with nulls into contiguous non-null runs — each run becomes its own path
// segment (mirrors Recharts' `connectNulls={false}`, which draws disconnected sub-lines
// rather than bridging gaps).
function contiguousRuns(points: (Point | null)[]): Point[][] {
  const runs: Point[][] = [];
  let current: Point[] = [];
  for (const p of points) {
    if (p) {
      current.push(p);
    } else if (current.length) {
      runs.push(current);
      current = [];
    }
  }
  if (current.length) runs.push(current);
  return runs;
}

/** Rounded pill label for the now-marker's current buy/sell price. */
function tag(key: string, x: number, y: number, text: string, color: string) {
  const w = text.length * 6.4 + 14;
  return (
    <g key={key}>
      <rect x={x + 7} y={y - 9} width={w} height={18} rx={6} fill={color} />
      <text
        x={x + 7 + w / 2}
        y={y + 3.5}
        textAnchor="middle"
        fontSize={11}
        fontWeight={700}
        fill="#fff"
        style={{ fontFamily: 'var(--font-heading)' }}
      >
        {text}
      </text>
    </g>
  );
}

interface HoverState {
  index: number;
  x: number; // container-relative px, for tooltip placement
  y: number;
  containerWidth: number;
}

interface ZoomRange {
  start: number;
  end: number;
}

export default function PriceChart({
  data,
  solarProfiles,
  solarForecast,
  dispatchSchedule,
  pastDispatchSlots,
  pastPlannedSocKwhByTime,
  startSocKwh,
  socIsLive,
  actualSocByTime,
  actualFlowsByTime,
  interventionsByTime,
  batteryKwh,
  skattOverforing,
  batteryFloorKwh,
}: Props) {
  const {
    actionBands,
    chartData,
    xTicks,
    firstTime,
    lastTime,
    tomorrowTime,
    beforeTomorrowTime,
    geometry,
    timeIndex,
    priceMax,
    priceMin,
    solarMax,
  } = useChartData(
    data,
    solarProfiles,
    solarForecast,
    dispatchSchedule,
    batteryKwh,
    skattOverforing,
    batteryFloorKwh,
    actualSocByTime,
    pastDispatchSlots,
    pastPlannedSocKwhByTime,
    actualFlowsByTime,
    interventionsByTime,
    startSocKwh,
  );

  const hasActualSoc = chartData.some((d) => d.actualSocPct != null);
  const hasPlan = !!dispatchSchedule;
  const containerRef = useRef<HTMLDivElement>(null);
  const containerWidth = useContainerWidth(containerRef);
  const isMobile = useMobileViewport();
  const geo = useMemo(() => containerWidth != null && containerWidth < 800 ? buildChartGeometry(containerWidth, 270) : geometry, [containerWidth, geometry]);

  const [layers, setLayers] = useState<ChartLayers>({ solar: true, soc: true, zones: true });
  const [zoomRange, setZoomRange] = useState<ZoomRange | null>(null);
  const [rangePreset, setRangePreset] = useState<'now' | 'today' | 'tomorrow' | 'all' | 'custom'>('now');
  const count = chartData.length;
  const fullEnd = Math.max(0, count - 1);
  const [nowTime, setNowTime] = useState<string | null>(null);
  useEffect(() => {
    const times = chartData.map((d) => d.time);
    const update = () => setNowTime(computeNowSlotTime(new Date(), times));
    update();
    const id = setInterval(update, 60_000);
    return () => clearInterval(id);
  }, [chartData]);

  const nowPoint = nowTime ? chartData.find((d) => d.time === nowTime) : undefined;
  const nowIndex = nowPoint ? timeIndex.get(nowPoint.time) : undefined;
  const tomorrowIndex = chartData.findIndex((point) => point.time.startsWith(data.tomorrow));
  const afterTomorrowIndex = chartData.findIndex((point) => point.time.slice(0, 10) > data.tomorrow);
  const todayEnd = tomorrowIndex > 0 ? tomorrowIndex - 1 : fullEnd;
  const tomorrowEnd = afterTomorrowIndex > 0 ? afterTomorrowIndex - 1 : fullEnd;
  const dayCount = new Set(chartData.map((point) => point.time.slice(0, 10))).size;
  // The first forecast slot is the exact handover from published day-ahead prices to
  // PlaneraElen's estimate. Keep this derived from provenance, not from a date boundary:
  // the forecast may begin part-way through a day when only some actual slots are available.
  const forecastStartIndex = chartData.findIndex((point) => point.priceSource === 'forecast');
  const hasMixedPriceSources = forecastStartIndex > 0;
  const defaultMobileRange = mobileChartWindow(nowIndex ?? 0, count);
  const effectiveZoom = zoomRange ?? (isMobile && rangePreset === 'now' ? defaultMobileRange : null);
  const viewStartIndex = Math.max(0, Math.min(effectiveZoom?.start ?? 0, fullEnd));
  const viewEndIndex = Math.max(viewStartIndex, Math.min(effectiveZoom?.end ?? fullEnd, fullEnd));
  const forecastStartVisible =
    hasMixedPriceSources && forecastStartIndex <= viewEndIndex;
  const forecastOverlayStartIndex = Math.max(viewStartIndex, forecastStartIndex);

  // Keep the live marker accurate without shifting a user-selected chart window.
  const nowVisible = nowIndex != null && nowIndex >= viewStartIndex && nowIndex <= viewEndIndex;

  // Everything the layers draw against: data, box, scales (see price-chart/frame.ts).
  const frame = useMemo(
    () => buildFrame(chartData, geo, timeIndex, priceMin, priceMax, solarMax, viewStartIndex, viewEndIndex),
    [chartData, geo, timeIndex, priceMin, priceMax, solarMax, viewStartIndex, viewEndIndex],
  );
  const { xAt, tx } = frame;

  const visibleXTicks = useMemo(() => {
    const ticks = xTicks.filter((tick) => {
      const index = timeIndex.get(tick);
      return index != null && index >= viewStartIndex && index <= viewEndIndex;
    });
    return isMobile && ticks.length > 6 ? ticks.filter((_, index) => index % Math.ceil(ticks.length / 6) === 0) : ticks;
  }, [xTicks, timeIndex, viewStartIndex, viewEndIndex, isMobile]);

  const buyPath = useMemo(
    () => buildStepLinePath(chartData.slice(viewStartIndex, viewEndIndex + 1).map((d, offset): Point => {
      const i = viewStartIndex + offset;
      return [xAt(i), priceYScale(d.buy, priceMax, geo, priceMin)];
    })),
    [chartData, viewStartIndex, viewEndIndex, priceMax, priceMin, geo, xAt],
  );
  const sellPath = useMemo(
    () => buildStepLinePath(chartData.slice(viewStartIndex, viewEndIndex + 1).map((d, offset): Point => {
      const i = viewStartIndex + offset;
      return [xAt(i), priceYScale(d.sell, priceMax, geo, priceMin)];
    })),
    [chartData, viewStartIndex, viewEndIndex, priceMax, priceMin, geo, xAt],
  );
  const solarTopPoints = useMemo(
    () => chartData.slice(viewStartIndex, viewEndIndex + 1).map((d, offset): Point => {
      const i = viewStartIndex + offset;
      return [xAt(i), solarYScale(d.solarKwh, solarMax, geo)];
    }),
    [chartData, viewStartIndex, viewEndIndex, solarMax, geo, xAt],
  );
  const solarAreaPath = useMemo(() => buildAreaPath(solarTopPoints, geo.baseY), [solarTopPoints, geo]);
  const solarLinePath = useMemo(() => buildLinePath(solarTopPoints), [solarTopPoints]);

  const socPlannedRuns = useMemo(
    () =>
      contiguousRuns(
        chartData.map((d, i): Point | null =>
          i < viewStartIndex || i > viewEndIndex || d.socPct == null ? null : [xAt(i), socYScale(d.socPct, geo)],
        ),
      ),
    [chartData, viewStartIndex, viewEndIndex, geo, xAt],
  );
  const socActualRuns = useMemo(
    () =>
      contiguousRuns(
        chartData.map((d, i): Point | null =>
          i < viewStartIndex || i > viewEndIndex || d.actualSocPct == null
            ? null
            : [xAt(i), socYScale(d.actualSocPct, geo)],
        ),
      ),
    [chartData, viewStartIndex, viewEndIndex, geo, xAt],
  );

  const peakSolarIdx = useMemo(() => {
    let best = -1;
    let bestVal = 0;
    chartData.forEach((d, i) => {
      if (i < viewStartIndex || i > viewEndIndex) return;
      if (d.solarKwh > bestVal) {
        bestVal = d.solarKwh;
        best = i;
      }
    });
    return best;
  }, [chartData, viewStartIndex, viewEndIndex]);

  // Hover detail (restores what the old Recharts <Tooltip> gave for free): a transparent
  // full-height hit area over the chart maps cursor x -> nearest slot index, independent of
  // the SVG's rendered size (it's scaled by viewBox, so we go through the container's actual
  // pixel width rather than SVG-space coordinates).
  const [hover, setHover] = useState<HoverState | null>(null);
  const pinchRef = useRef<{ distance: number; centerFraction: number; viewStart: number; viewEnd: number } | null>(null);

  const touchDistance = (a: React.Touch, b: React.Touch) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  const touchCenterFraction = (a: React.Touch, b: React.Touch, rect: DOMRect) =>
    Math.min(1, Math.max(0, ((a.clientX + b.clientX) / 2 - rect.left) / rect.width));

  const updateHover = (clientX: number, clientY: number) => {
    const el = containerRef.current;
    if (!el || count === 0) return;
    const rect = el.getBoundingClientRect();
    const localX = clientX - rect.left;
    const localY = clientY - rect.top;
    const fraction = Math.min(1, Math.max(0, localX / rect.width));
    const viewBoxX = fraction * geo.width;
    const idxFloat = viewStartIndex + ((viewBoxX - geo.padL) / geo.plotW) * (viewEndIndex - viewStartIndex);
    const index = Math.min(count - 1, Math.max(0, Math.round(idxFloat)));
    setHover({ index, x: localX, y: localY, containerWidth: rect.width });
  };
  const clearHover = () => setHover(null);

  const handleWheel = (event: React.WheelEvent<HTMLDivElement>) => {
    if (count < 2) return;
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const currentSpan = Math.max(1, viewEndIndex - viewStartIndex);
    const maxSpan = Math.max(1, fullEnd);
    const requestedSpan = Math.round(currentSpan * (event.deltaY < 0 ? 0.75 : 1.33));
    const nextSpan = Math.max(8, Math.min(maxSpan, requestedSpan));
    if (nextSpan >= maxSpan) {
      setRangePreset('all');
      setZoomRange({ start: 0, end: fullEnd });
      return;
    }
    const cursorIndex = viewStartIndex + fraction * currentSpan;
    let nextStart = Math.round(cursorIndex - fraction * nextSpan);
    nextStart = Math.max(0, Math.min(nextStart, fullEnd - nextSpan));
    setRangePreset('custom');
    setZoomRange({ start: nextStart, end: nextStart + nextSpan });
  };

  const setVisibleRange = (start: number, end: number, preset: 'today' | 'tomorrow' | 'all') => {
    setRangePreset(preset);
    setZoomRange({ start, end });
    setHover(null);
  };

  const hoverPoint = hover ? chartData[hover.index] : null;

  // The decision zone the hovered slot belongs to, for the tooltip's zone-total row. Bands are
  // few (a handful per day), so a linear scan per hover render is fine.
  const hoverBand =
    hoverPoint && hoverPoint.decision
      ? actionBands.find(
          (b) => b.kind === hoverPoint.decision && b.x1 <= hoverPoint.time && hoverPoint.time <= b.x2,
        ) ?? null
      : null;

  // Zone-level actual total for the hovered band (buy/sell only, matching the in-chart ⚠ marker
  // and the tooltip's "Verkligt (hela zonen)" row) — bands are few, a linear scan is fine.
  const hoverBandActual =
    hoverBand && hoverBand.kind !== 'hold' ? sumActualForBand(hoverBand, chartData, timeIndex) : null;

  return (
    <div
      className="card-surface box-border flex min-w-0 flex-col p-4 min-[700px]:p-5 order-1 min-[700px]:col-span-2 min-[1600px]:order-none min-[1600px]:[grid-column:1/span_2] min-[1600px]:[grid-row:1]"
      style={{ fontFamily: 'var(--font-body)' }}
    >
      <div className="mb-3.5 flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <div
            className="flex h-[26px] w-[26px] items-center justify-center rounded-lg"
            style={{ background: 'var(--badge-bg)', color: 'var(--badge-color)' }}
          >
            <svg viewBox="0 0 24 24" style={{ width: 15, height: 15 }}>
              <path
                d="M3 17 L9 11 L13 15 L21 6 M15.5 6 H21 V11.5"
                fill="none"
                stroke="currentColor"
                strokeWidth={2.2}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <div className="flex flex-col gap-px">
            <div className="text-[17px] font-bold leading-tight tracking-tight min-[700px]:text-[15px]" style={{ fontFamily: 'var(--font-heading)' }}>
              Elpriser
            </div>
            <div className="text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>
              {data.today} · {dayCount} dygn
              {hasMixedPriceSources && ' · inklusive prognos'}
              {!data.hasTomorrow && tomorrowIndex > 0 && (
                <span className="ml-2" style={{ color: 'var(--color-now)' }}>
                  · Imorgon: prognos (spotpris ännu ej publicerat)
                </span>
              )}
            </div>
          </div>
        </div>
        {nowPoint && (
          <div className="flex items-center gap-2 rounded-xl px-3 py-1.5" style={{ background: 'var(--badge-bg)' }}>
            <span className="h-2 w-2 rounded-full" style={{ background: 'var(--color-now)' }} />
            <span className="text-[13px] font-semibold" style={{ color: 'var(--text-muted)' }}>
              Nu
            </span>
            <span className="text-[13px] font-bold" style={{ fontFamily: 'var(--font-heading)', color: 'var(--color-buy)' }}>
              {Math.round(nowPoint.buy)}
            </span>
            <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
              /
            </span>
            <span className="text-[13px] font-bold" style={{ fontFamily: 'var(--font-heading)', color: 'var(--color-sell)' }}>
              {Math.round(nowPoint.sell)}
            </span>
            <span className="text-[12px]" style={{ color: 'var(--text-muted)' }}>
              öre
            </span>
          </div>
        )}
      </div>

      <ChartLegend layers={layers} setLayers={setLayers} hasPlan={hasPlan} hasMixedPriceSources={hasMixedPriceSources} />

      <div className="mb-2 flex flex-wrap items-center justify-start gap-1.5 min-[700px]:justify-end" aria-label="Zooma grafens tidsintervall">
        <span className="mr-1 hidden text-[11px] font-bold uppercase tracking-wide min-[700px]:inline" style={{ color: 'var(--text-muted)' }}>
          Zoom
        </span>
        <button type="button" className="chip min-h-11 rounded-xl px-2.5 text-[13px] font-semibold min-[700px]:hidden"
          aria-label="12 timmar kring nu"
          style={rangePreset === 'now' && zoomRange == null ? { border: '1px solid var(--divider)' } : { color: 'var(--text-muted)' }}
          onClick={() => { setRangePreset('now'); setZoomRange(null); setHover(null); }}>
          12 h
        </button>
        <button
          type="button"
          className="chip min-h-11 rounded-xl px-3 text-[13px] font-semibold min-[900px]:min-h-0 min-[700px]:py-1"
          style={rangePreset === 'all' || (!isMobile && zoomRange == null) ? { border: '1px solid var(--divider)' } : { color: 'var(--text-muted)', opacity: 0.75 }}
          onClick={() => setVisibleRange(0, fullEnd, 'all')}
        >
          {dayCount === 3 ? '3 dygn' : 'Hela'}
        </button>
        <button
          type="button"
          className="chip min-h-11 rounded-xl px-3 text-[13px] font-semibold min-[900px]:min-h-0 min-[700px]:py-1"
          style={rangePreset === 'today' && zoomRange?.start === 0 && zoomRange?.end === todayEnd ? { border: '1px solid var(--divider)' } : { color: 'var(--text-muted)', opacity: 0.65 }}
          onClick={() => setVisibleRange(0, todayEnd, 'today')}
        >
          Idag
        </button>
        {tomorrowIndex > 0 && (
          <button
            type="button"
            className="chip min-h-11 rounded-xl px-3 text-[13px] font-semibold min-[900px]:min-h-0 min-[700px]:py-1"
            style={rangePreset === 'tomorrow' && zoomRange?.start === tomorrowIndex && zoomRange?.end === tomorrowEnd ? { border: '1px solid var(--divider)' } : { color: 'var(--text-muted)', opacity: 0.65 }}
            onClick={() => setVisibleRange(tomorrowIndex, tomorrowEnd, 'tomorrow')}
          >
            Imorgon
          </button>
        )}
        <span className="ml-1 hidden text-[11px] min-[900px]:inline" style={{ color: 'var(--text-muted)' }}>
          Mushjul för stegvis zoom
        </span>
      </div>

      <div
        ref={containerRef}
        className="relative rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2"
        tabIndex={0}
        role="group"
        aria-label="Prisgraf. Använd piltangenter för att välja kvart eller tryck på grafen."
        onKeyDown={(e) => {
          if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
          e.preventDefault();
          const index = Math.max(viewStartIndex, Math.min(viewEndIndex, (hover?.index ?? viewStartIndex) + (e.key === 'ArrowRight' ? 1 : -1)));
          const width = containerRef.current?.clientWidth ?? geo.width;
          setHover({ index, x: xAt(index) / geo.width * width, y: 50, containerWidth: width });
        }}
        // pan-y (not 'none'): lets the browser handle normal vertical page scroll when a
        // single finger starts on the chart — 'none' used to trap scrolling inside this card,
        // forcing a user to lift their finger and swipe outside it to keep scrolling the page.
        // The 2-finger pinch-zoom handler below still calls preventDefault() itself, so pan-y
        // doesn't interfere with pinch.
        style={{ touchAction: 'pan-y' }}
        onMouseMove={(e) => { if (!isMobile) updateHover(e.clientX, e.clientY); }}
        onMouseLeave={() => { if (!isMobile) clearHover(); }}
        onClick={(e) => { if (isMobile) updateHover(e.clientX, e.clientY); }}
        onTouchStart={(e) => {
          // Single-finger touch: show the hover/tooltip immediately on first contact, not only
          // once the finger moves (onTouchMove) — a still tap used to show nothing at all.
          if (e.touches.length === 1) updateHover(e.touches[0].clientX, e.touches[0].clientY);
          if (e.touches.length < 2 || count < 2) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const [a, b] = [e.touches[0], e.touches[1]];
          pinchRef.current = {
            distance: touchDistance(a, b),
            centerFraction: touchCenterFraction(a, b, rect),
            viewStart: viewStartIndex,
            viewEnd: viewEndIndex,
          };
          setHover(null);
        }}
        onTouchMove={(e) => {
          if (e.touches.length >= 2 && pinchRef.current) {
            e.preventDefault();
            const [a, b] = [e.touches[0], e.touches[1]];
            const range = pinchZoomRange(
              pinchRef.current.viewStart,
              pinchRef.current.viewEnd,
              fullEnd,
              pinchRef.current.distance,
              touchDistance(a, b),
              pinchRef.current.centerFraction,
            );
            setRangePreset('custom');
            setZoomRange(range);
            return;
          }
          if (e.touches[0]) updateHover(e.touches[0].clientX, e.touches[0].clientY);
        }}
        onTouchEnd={(e) => {
          if (e.touches.length < 2) pinchRef.current = null;
          if (e.touches.length === 0 && !isMobile) clearHover();
        }}
        onWheel={handleWheel}
      >
        <svg viewBox={`0 0 ${geo.width} ${geo.height}`} width="100%" style={{ display: 'block', height: 'auto', overflow: 'visible' }}>
          <DayZones
            frame={frame}
            firstTime={firstTime}
            lastTime={lastTime}
            tomorrowTime={tomorrowTime}
            beforeTomorrowTime={beforeTomorrowTime}
          />

          {layers.zones && <MinorChargeBands frame={frame} />}
          {layers.zones && <DecisionBands frame={frame} bands={actionBands} />}

          <PriceAxis frame={frame} />
          {hasPlan && layers.soc && <SocAxis frame={frame} />}

          {/* solar area */}
          {layers.solar && (
            <>
              <path d={solarAreaPath} fill="color-mix(in srgb, var(--color-solar) 15%, transparent)" stroke="none" />
              <path d={solarLinePath} fill="none" stroke="color-mix(in srgb, var(--color-solar) 55%, transparent)" strokeWidth={1.2} />
              {peakSolarIdx >= 0 && (
                <text
                  x={xAt(peakSolarIdx)}
                  y={solarTopPoints[peakSolarIdx - viewStartIndex][1] - 6}
                  textAnchor="middle"
                  fontSize={9}
                  fontWeight={700}
                  fill="color-mix(in srgb, var(--color-solar) 80%, transparent)"
                  style={{ fontFamily: 'var(--font-body)' }}
                >
                  Sol
                </text>
              )}
            </>
          )}

          {/* SoC lines — planned dashed, actual solid on top */}
          {hasPlan &&
            layers.soc &&
            socPlannedRuns.map((run, i) => (
              <path
                key={`planned-${i}`}
                d={buildLinePath(run)}
                fill="none"
                stroke="color-mix(in srgb, var(--color-soc) 75%, transparent)"
                strokeWidth={1.8}
                strokeDasharray="6 5"
                strokeLinecap="round"
              />
            ))}
          {hasPlan &&
            layers.soc &&
            hasActualSoc &&
            socActualRuns.map((run, i) => (
              <path key={`actual-${i}`} d={buildLinePath(run)} fill="none" stroke="var(--color-soc)" strokeWidth={2.4} strokeLinecap="round" />
            ))}

          {/* actual/forecast handover — provenance boundary, independent of the day divider */}
          {forecastStartVisible && (() => {
            const xf = xAt(forecastOverlayStartIndex);
            if (xf == null) return null;
            return (
              <g pointerEvents="none">
                <rect
                  x={xf}
                  y={geo.padT}
                  width={Math.max(0, geo.padL + geo.plotW - xf)}
                  height={geo.baseY - geo.padT}
                  fill="var(--color-now)"
                  opacity={0.045}
                />
                <line
                  x1={xf}
                  y1={geo.padT}
                  x2={xf}
                  y2={geo.baseY}
                  stroke="var(--color-now)"
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  opacity={0.9}
                />
                <rect x={xf + 5} y={geo.padT + 2} width={92} height={17} rx={5} fill="var(--color-now)" opacity={0.94} />
                <text x={xf + 51} y={geo.padT + 14} textAnchor="middle" fontSize={9.5} fontWeight={700} fill="var(--color-now-text)" style={{ fontFamily: 'var(--font-body)' }}>
                  Prognos börjar
                </text>
              </g>
            );
          })()}

          {/* price lines — sell then buy, buy on top */}
          <path
            d={sellPath}
            fill="none"
            stroke="var(--color-sell)"
            strokeWidth={1.0}
            strokeLinejoin="round"
            strokeLinecap="round"
            style={{ filter: 'drop-shadow(0 1px 4px color-mix(in srgb, var(--color-sell) 35%, transparent))' }}
          />
          <path
            d={buyPath}
            fill="none"
            stroke="var(--color-buy)"
            strokeWidth={1.0}
            strokeLinejoin="round"
            strokeLinecap="round"
            style={{ filter: 'drop-shadow(0 1px 5px color-mix(in srgb, var(--color-buy) 40%, transparent))' }}
          />

          {/* today/tomorrow divider + labels */}
          {tomorrowTime && (
            <line
              x1={tx(tomorrowTime) ?? 0}
              y1={geo.padT}
              x2={tx(tomorrowTime) ?? 0}
              y2={geo.baseY}
              stroke="var(--axis-text)"
              strokeWidth={1.2}
              strokeDasharray="4 5"
              opacity={0.7}
            />
          )}
          {firstTime && (
            <text x={(tx(firstTime) ?? 0) + 4} y={geo.padT + 13} fontSize={10} fontWeight={700} fill="var(--axis-text)" style={{ fontFamily: 'var(--font-body)' }}>
              Idag
            </text>
          )}
          {tomorrowTime && (
            <text x={(tx(tomorrowTime) ?? 0) + 4} y={geo.padT + 13} fontSize={10} fontWeight={700} fill="var(--axis-text)" style={{ fontFamily: 'var(--font-body)' }}>
              Imorgon
            </text>
          )}

          {/* now marker */}
          {nowTime &&
            nowPoint &&
            nowVisible &&
            (() => {
              const xn = tx(nowTime);
              if (xn == null) return null;
              const buyNowV = Math.round(nowPoint.buy);
              const sellNowV = Math.round(nowPoint.sell);
              const byY = priceYScale(nowPoint.buy, priceMax, geo, priceMin);
              let syY = priceYScale(nowPoint.sell, priceMax, geo, priceMin);
              if (Math.abs(byY - syY) < 20) syY = byY + 20;
              return (
                <g>
                  <line x1={xn} y1={geo.padT} x2={xn} y2={geo.baseY} stroke="var(--color-now)" strokeWidth={2} />
                  {tag('tagB', xn, byY, `Köp ${buyNowV}`, 'var(--color-buy)')}
                  {tag('tagS', xn, syY, `Sälj ${sellNowV}`, 'var(--color-sell)')}
                  <rect x={xn - 15} y={geo.padT - 22} width={30} height={16} rx={6} fill="var(--color-now)" />
                  <text x={xn} y={geo.padT - 10.5} textAnchor="middle" fontSize={10} fontWeight={700} fill="var(--color-now-text)" style={{ fontFamily: 'var(--font-heading)' }}>
                    Nu
                  </text>
                </g>
              );
            })()}

          <TimeAxis frame={frame} ticks={visibleXTicks} />

          {/* hover guide + per-series dots */}
          {hover && hoverPoint && (
            <g pointerEvents="none">
              <line x1={xAt(hover.index)} y1={geo.padT} x2={xAt(hover.index)} y2={geo.baseY} stroke="var(--text-muted)" strokeWidth={1} strokeDasharray="3 3" opacity={0.6} />
              <circle cx={xAt(hover.index)} cy={priceYScale(hoverPoint.sell, priceMax, geo, priceMin)} r={3.5} fill="var(--color-sell)" stroke="var(--card-bg)" strokeWidth={1.5} />
              <circle cx={xAt(hover.index)} cy={priceYScale(hoverPoint.buy, priceMax, geo, priceMin)} r={3.5} fill="var(--color-buy)" stroke="var(--card-bg)" strokeWidth={1.5} />
              {hasPlan && layers.soc && hoverPoint.socPct != null && (
                <circle cx={xAt(hover.index)} cy={socYScale(hoverPoint.socPct, geo)} r={3.5} fill="var(--color-soc)" stroke="var(--card-bg)" strokeWidth={1.5} />
              )}
            </g>
          )}
        </svg>

        {!isMobile && hover && hoverPoint && (
          <ChartTooltip
            point={hoverPoint}
            band={hoverBand}
            bandActual={hoverBandActual}
            placement={{ x: hover.x, y: hover.y, containerWidth: hover.containerWidth }}
            hasPlan={hasPlan}
          />
        )}
      </div>

      {isMobile && (hover && hoverPoint ? (
        <div className="mt-3" aria-live="polite">
          <ChartTooltip point={hoverPoint} band={hoverBand} bandActual={hoverBandActual}
            placement={{ x: hover.x, y: hover.y, containerWidth: hover.containerWidth }} hasPlan={hasPlan} inline />
        </div>
      ) : <p className="mt-2 text-[13px]" style={{ color: 'var(--text-muted)' }}>Tryck på grafen för pris och batteriplan per kvart.</p>)}

      {hasPlan && (
        <p className="mt-3 text-[13px]" style={{ color: 'var(--text-muted)' }}>
          <span className="min-[700px]:hidden">
            {socIsLive && startSocKwh != null
              ? `Planen utgår från ${Math.round((startSocKwh / batteryKwh) * 100)} % batteri.`
              : 'Planen utgår från antaget 50 % (ingen live-SoC).'}
          </span>
          <span className="hidden min-[700px]:inline">
            {socIsLive && startSocKwh != null
              ? `Batteri-rekommendationer baserade på aktuell laddning ${Math.round(
                  (startSocKwh / batteryKwh) * 100,
                )} % (${startSocKwh.toFixed(1)} kWh).`
              : 'Batteri-rekommendationer baserade på antaget 50 % (växelriktardata saknas).'}
          </span>
        </p>
      )}
    </div>
  );
}
