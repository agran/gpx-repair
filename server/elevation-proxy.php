<?php
/**
 * elevation-proxy.php
 * ---------------------------------------------------------------------------
 * Reverse proxy for the two elevation services used by gpx-repair
 * (https://agran.github.io/gpx-repair/):
 *
 *   - Open-Meteo Elevation API   https://api.open-meteo.com/v1/elevation
 *   - OpenTopoData SRTM30m       https://api.opentopodata.org/v1/srtm30m
 *
 * Only these two are proxied — routing (OSRM) is left untouched, since the
 * connectivity problem is specific to the elevation services.
 *
 * Deploy this file on a server that can reach an outbound HTTP proxy by IP
 * (configured below via OUTBOUND_PROXY). The browser calls THIS script,
 * which in turn fetches the real upstream through the outbound proxy and
 * relays the response back with permissive CORS headers.
 *
 * Usage from the browser:
 *   GET /elevation-proxy.php?url=<url-encoded full upstream URL>
 *
 * Only URLs whose prefix matches $ALLOWED_PREFIXES are forwarded — anything
 * else is rejected with 400, so this cannot be abused as an open proxy.
 * ---------------------------------------------------------------------------
 */

// ─── Configuration ──────────────────────────────────────────────────────────

// Outbound HTTP proxy that upstream requests are routed through.
// Set to '' (empty string) to disable proxying and call upstream directly.
const OUTBOUND_PROXY = '77.73.71.234:48484';

// Origins allowed to call this proxy (CORS). Add more if you serve the app
// from another domain, or '*' to allow everyone (fine for read-only, public,
// rate-limited data like elevation/routing).
const ALLOWED_ORIGINS = [
    'https://agran.github.io',
    'http://localhost',
    'http://127.0.0.1',
    // 'null' — браузер шлёт именно эту строку как Origin, когда страница
    // открыта локально как file:// (например, при тестировании index.html
    // без веб-сервера). Оставлено для удобства локальной разработки.
    'null',
];

// Only these upstream URL prefixes may be requested through this proxy.
const ALLOWED_PREFIXES = [
    'https://api.open-meteo.com/v1/elevation',
    'https://api.opentopodata.org/v1/srtm30m',
];

const REQUEST_TIMEOUT_S = 12;

// ─── CORS ───────────────────────────────────────────────────────────────────

$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
$originAllowed = false;
foreach (ALLOWED_ORIGINS as $allowed) {
    if ($allowed === '*' || $origin === $allowed) {
        $originAllowed = true;
        break;
    }
}
header('Access-Control-Allow-Origin: ' . ($originAllowed ? $origin : 'null'));
header('Vary: Origin');
header('Access-Control-Allow-Methods: GET, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
header('Access-Control-Max-Age: 86400');

// Preflight — nothing else to do.
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Only GET is supported']);
    exit;
}

// ─── Validate target URL ────────────────────────────────────────────────────

$target = $_GET['url'] ?? '';
if ($target === '') {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Missing "url" query parameter']);
    exit;
}

$isAllowed = false;
foreach (ALLOWED_PREFIXES as $prefix) {
    if (str_starts_with($target, $prefix)) {
        $isAllowed = true;
        break;
    }
}
if (!$isAllowed) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Target URL is not in the allowlist']);
    exit;
}

// ─── Fetch upstream through the outbound proxy ─────────────────────────────

$ch = curl_init($target);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => true,
    CURLOPT_MAXREDIRS      => 3,
    CURLOPT_TIMEOUT        => REQUEST_TIMEOUT_S,
    CURLOPT_CONNECTTIMEOUT => 5,
    CURLOPT_HTTPHEADER     => [
        'Accept: application/json',
        'User-Agent: gpx-repair-elevation-proxy/1.0',
    ],
]);
if (OUTBOUND_PROXY !== '') {
    curl_setopt($ch, CURLOPT_PROXY, OUTBOUND_PROXY);
    curl_setopt($ch, CURLOPT_PROXYTYPE, CURLPROXY_HTTP);
}

$body = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$contentType = curl_getinfo($ch, CURLINFO_CONTENT_TYPE) ?: 'application/json';
$curlError = curl_error($ch);
curl_close($ch);

if ($body === false) {
    http_response_code(502);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Upstream request failed', 'detail' => $curlError]);
    exit;
}

http_response_code($httpCode ?: 502);
header('Content-Type: ' . $contentType);
echo $body;
