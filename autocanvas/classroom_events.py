"""Read-only classroom event projection; independent of detection and HTTP adapters."""
import hashlib
import json
from pathlib import Path


def classroom_events(root: Path):
    base = root/'outputs'
    events, scans = [], []

    def safe(path):
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(base.resolve())

    for manifest in sorted(base.glob('*/*/vod_slides/result/slides.json')):
        if not safe(manifest):
            continue
        relative = manifest.relative_to(root).parts
        context = {'course_id': relative[1], 'lecture_id': relative[2], 'source': 'vod'}
        scan = manifest.with_name('qr.json')
        report = {'status': 'not_scanned', 'events': []}
        if safe(scan):
            try:
                report = json.loads(scan.read_text())
                if not isinstance(report, dict) or not isinstance(report.get('events'), list):
                    raise ValueError('Invalid report')
            except (ValueError, OSError):
                report = {'status': 'failed', 'events': []}
        # Legacy v1 reports may contain unverified OpenCV boxes. Never present
        # those as confirmed QR observations, even before a local rescan.
        report['events'] = [row for row in report['events'] if isinstance(row, dict)
                            and row.get('decoded') is True and isinstance(row.get('content'), str)
                            and row['content']]
        scans.append({**context, 'status': report.get('status', 'failed'),
                      'scanned_images': report.get('scanned_images', 0),
                      'total_images': report.get('total_images', 0), 'count': len(report['events'])})
        for row in report['events']:
            if not isinstance(row, dict) or not isinstance(row.get('start'), (int, float)):
                continue
            name = str(row.get('image', ''))
            image = manifest.parent/name
            if Path(name).name != name or not safe(image):
                continue
            events.append({**context, 'type': 'qr', 'id': f'{relative[1]}:{relative[2]}:qr:{row.get("id", name)}',
                           'start': row['start'], 'content': row.get('content', ''),
                           'decoded': bool(row.get('decoded')), 'image': image.relative_to(root).as_posix()})
    for path in sorted(base.glob('*/*/live/events.jsonl')):
        if not safe(path):
            continue
        parts = path.relative_to(root).parts
        with path.open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if row.get('type') != 'keyword' or not isinstance(row.get('start'), (int, float)):
                        continue
                    identity = hashlib.sha256(line.encode()).hexdigest()[:24]
                    events.append({'id': f'{parts[1]}:{parts[2]}:keyword:{identity}',
                                   'type': 'keyword', 'source': 'live', 'course_id': parts[1], 'lecture_id': parts[2],
                                   'start': row['start'], 'keyword': row.get('keyword', ''), 'text': row.get('text', '')})
                except (ValueError, AttributeError):
                    continue
    return {'events': list({row['id']: row for row in events}.values()), 'scans': scans}
