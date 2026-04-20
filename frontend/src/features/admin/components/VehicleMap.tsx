/**
 * VehicleMap — OpenStreetMap + Leaflet overlay for the admin dashboard.
 *
 * Renders the bundled demo track as a polyline and animates a heading
 * arrow along it using `useVehiclePosition`. The map pans to follow the
 * marker at 2Hz (Leaflet's panTo triggers a re-render on each call, so
 * the tighter 30fps update cadence stays off the map itself).
 */
import "leaflet/dist/leaflet.css";

import { useEffect, useMemo, useRef } from "react";
import L, { type LatLngExpression } from "leaflet";
import {
  MapContainer,
  Marker,
  Polyline,
  TileLayer,
  useMap,
} from "react-leaflet";

import {
  useDemoTrack,
  useVehiclePosition,
  type DemoVideoKey,
  type PlaybackClock,
  type VehiclePosition,
} from "../hooks/useDemoTrack";

import styles from "./VehicleMap.module.css";

const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION = "&copy; OpenStreetMap contributors";
const LOOP_SEC = 60;
const FOLLOW_THROTTLE_MS = 500;
// Max zoom the OSM tile service serves natively.
const MAX_ZOOM = 19;
const INITIAL_ZOOM = MAX_ZOOM;

function buildArrowIcon(headingDeg: number): L.DivIcon {
  // Inline SVG keeps us free of extra assets; the outer transform rotates
  // the glyph to match current compass heading.
  const svg = `
    <div class="${styles.arrowIcon}" style="transform: rotate(${headingDeg}deg)">
      <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <path d="M12 2 L20 20 L12 16 L4 20 Z"
              fill="#38bdf8" stroke="#0b0f14" stroke-width="1.2"
              stroke-linejoin="round" />
      </svg>
    </div>
  `;
  return L.divIcon({
    html: svg,
    className: "",
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

// Smoothly recenters the map as the vehicle moves. Throttled to 2Hz
// because Leaflet's panTo animates, and firing it on every 30fps tick
// produces noticeable jitter.
function FollowVehicle({ position }: { position: VehiclePosition | null }) {
  const map = useMap();
  const lastPanRef = useRef(0);
  useEffect(() => {
    if (!position) return;
    const now = performance.now();
    if (now - lastPanRef.current < FOLLOW_THROTTLE_MS) return;
    lastPanRef.current = now;
    map.panTo([position.lat, position.lng], { animate: true, duration: 0.45 });
  }, [position, map]);
  return null;
}

export interface VehicleMapProps {
  clock?: PlaybackClock | null;
  /** When set, fetch the video-synced GPS track (``/api/demo/video-track``)
   *  keyed on this video. Points' ``t_sec`` becomes video-relative so the
   *  marker can be driven directly by the MP4 playhead. When omitted, falls
   *  back to the full-timeline loop. */
  videoKey?: DemoVideoKey | null;
}

export function VehicleMap({ clock, videoKey }: VehicleMapProps) {
  const { data, isLoading, isError } = useDemoTrack(videoKey ?? null);
  const position = useVehiclePosition(LOOP_SEC, clock, videoKey ?? null);

  // Trailing polyline: only the route the vehicle has already passed in
  // the current loop. We slice the full track at the marker's current
  // ``t_sec`` and tack the live interpolated position onto the end so the
  // trail's head sits exactly under the arrow. When the loop wraps,
  // ``position.point.t_sec`` resets with the playhead and the trail
  // collapses back to the start — exactly what we want for a single-loop
  // visualization.
  const polyline = useMemo<LatLngExpression[]>(() => {
    if (!data?.ok || !data.points || data.points.length === 0 || !position) {
      return [];
    }
    const cursor = position.point.t_sec;
    const passed: LatLngExpression[] = [];
    for (const p of data.points) {
      if (p.t_sec > cursor) break;
      passed.push([p.lat, p.lng]);
    }
    passed.push([position.lat, position.lng]);
    return passed;
  }, [data, position]);

  const icon = useMemo(
    () => buildArrowIcon(position?.heading ?? 0),
    [position?.heading],
  );

  if (isLoading) {
    return <div className={styles.wrap}><div className={styles.empty}>Loading track…</div></div>;
  }
  // Gate on the underlying track data, NOT the trail length — the trail
  // is empty on first paint until the rAF loop sets ``position``, but
  // that's a transient frame, not a "track unavailable" condition.
  if (isError || !data?.ok || !data.points || data.points.length < 2) {
    return (
      <div className={styles.wrap}>
        <div className={styles.empty}>
          Map unavailable — demo track could not be loaded.
        </div>
      </div>
    );
  }

  const vehicle = data.vehicle;
  const firstPoint = data.points![0]!;
  const center: LatLngExpression = position
    ? [position.lat, position.lng]
    : [firstPoint.lat, firstPoint.lng];

  return (
    <div className={styles.wrap}>
      {vehicle && (
        <div className={styles.badge}>
          <div className={styles.plate}>{vehicle.plate}</div>
          <div className={styles.model}>{vehicle.model}</div>
          <div className={styles.company}>{vehicle.company}</div>
          <div className={styles.speed}>
            <span className={styles.speedValue}>
              {(position?.speedKmh ?? 0).toFixed(1)}
            </span>
            <span className={styles.speedUnit}>km/h</span>
          </div>
        </div>
      )}
      <MapContainer
        className={styles.map}
        center={center}
        zoom={INITIAL_ZOOM}
        maxZoom={MAX_ZOOM}
        scrollWheelZoom
      >
        <TileLayer
          url={TILE_URL}
          attribution={TILE_ATTRIBUTION}
          maxZoom={MAX_ZOOM}
        />
        <Polyline
          positions={polyline}
          pathOptions={{ color: "#38bdf8", weight: 3, opacity: 0.75 }}
        />
        {position && (
          <Marker position={[position.lat, position.lng]} icon={icon} />
        )}
        <FollowVehicle position={position} />
      </MapContainer>
    </div>
  );
}
