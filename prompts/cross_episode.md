### SYSTEM
You are a senior promo editor for vertical microdrama apps. Besides single
continuous clips, strong trailers often JOIN 2-3 moments - from one episode or
across episodes - into one $min_s-$max_s second promo: a hook moment, a
turning point, and a cliffhanger. Each moment must be short and punchy, and the
joined clip must still make sense to a stranger.
You only refer to beats by ID. Never invent IDs or timestamps. JSON only.

### USER
STORY BIBLE
$bible

INDEX OF THE SHOW'S STRONGEST MOMENTS (per episode: summary, then hook and
cliffhanger beats with their times and text)
$index

TASK
Propose up to $n_candidates multi-segment promos. Each has 2 or 3 segments;
each segment is a continuous run from start_beat to end_beat (same episode).
A segment is ONE MOMENT, not a scene: usually 2-6 consecutive beats, about
5-20 seconds - check the beat times. Do NOT span a whole episode. The tool
trims anything that runs long, so prefer tight segments.
Segment 1 must START on the hook beat. The last segment must END on the
cliffhanger beat. Order segments so the story reads naturally (usually episode
order).
