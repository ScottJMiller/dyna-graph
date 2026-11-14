// Optional, dependency-free geometry regression checks for the offline atlas.
// Run: node --test docs/testing/architecture-routing.mjs
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../architecture.html', import.meta.url), 'utf8');
const data = JSON.parse(html.match(/<script id="architecture-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const code = html.match(/\/\* CONNECTOR ROUTER START[\s\S]*?\/\* CONNECTOR ROUTER END \*\//)[0];
const api = {};
runInNewContext(code, api);
const width = 280, height = 112;
const headers = data.groups.map(g => ({x:g.x+10,y:g.y+12,w:Math.min(g.w-20,g.label.length*8+55),h:22}));
headers.push({x:60,y:2264,w:1240,h:54});
const result = api.routeConnections(data.nodes, data.edges, width, height, headers);

function segments(routes) {
  return routes.flatMap((r, edge) => r.points.slice(1).map((b, i) => ({a:r.points[i], b, edge})));
}
function shared(a, b) {
  if (a.a.y===a.b.y && b.a.y===b.b.y && a.a.y===b.a.y)
    return Math.max(Math.min(a.a.x,a.b.x),Math.min(b.a.x,b.b.x)) <= Math.min(Math.max(a.a.x,a.b.x),Math.max(b.a.x,b.b.x));
  if (a.a.x===a.b.x && b.a.x===b.b.x && a.a.x===b.a.x)
    return Math.max(Math.min(a.a.y,a.b.y),Math.min(b.a.y,b.b.y)) <= Math.min(Math.max(a.a.y,a.b.y),Math.max(b.a.y,b.b.y));
  return false;
}
function through(s, n) {
  return s.a.y===s.b.y ? s.a.y>n.y && s.a.y<n.y+height && Math.max(Math.min(s.a.x,s.b.x),n.x)<Math.min(Math.max(s.a.x,s.b.x),n.x+width)
    : s.a.x>n.x && s.a.x<n.x+width && Math.max(Math.min(s.a.y,s.b.y),n.y)<Math.min(Math.max(s.a.y,s.b.y),n.y+height);
}
function noSharedTracks(routes) {
  const lines=segments(routes);
  for(let i=0;i<lines.length;i++) for(let j=0;j<i;j++) {
    if(lines[i].edge!==lines[j].edge) assert(!shared(lines[i],lines[j]),`Shared track: connectors ${lines[i].edge} / ${lines[j].edge}`);
  }
}

test('every component supplies the fields needed to render and inspect it', () => {
  const ids = new Set();
  for (const node of data.nodes) {
    assert(!ids.has(node.id), `Duplicate component: ${node.id}`);
    ids.add(node.id);
    for (const field of ['id', 'label', 'summary', 'description', 'kind', 'inputs', 'outputs', 'notes']) {
      assert.equal(typeof node[field], 'string', `${node.id}: missing ${field}`);
      assert(node[field].trim(), `${node.id}: empty ${field}`);
    }
    assert.equal(typeof node.path, 'string', `${node.id}: invalid path`);
    assert(Array.isArray(node.interfaces), `${node.id}: missing interfaces`);
    assert(node.interfaces.every(value => typeof value === 'string'), `${node.id}: invalid interface`);
    assert(data.phases.some(phase => phase.id === node.phase), `${node.id}: unknown phase`);
    assert.equal(typeof node.support, 'boolean', `${node.id}: missing support flag`);
    assert(Number.isFinite(node.x) && Number.isFinite(node.y), `${node.id}: invalid position`);
  }
  for (const edge of data.edges) {
    assert(ids.has(edge.source) && ids.has(edge.target), `Unknown endpoint: ${edge.source} / ${edge.target}`);
  }
});

test('every connector reaches its two unique component-perimeter ports', () => {
  assert.equal(result.routes.length,data.edges.length);
  const byId=new Map(data.nodes.map(n=>[n.id,n])), used=new Map(data.nodes.map(n=>[n.id,new Set()]));
  const sides=new Set();
  for(let i=0;i<data.edges.length;i++) {
    const e=data.edges[i], r=result.routes[i];
    for(const [id,p,side] of [[e.source,r.sourcePort,r.sourceSide],[e.target,r.targetPort,r.targetSide]]) {
      const n=byId.get(id), key=`${p.x},${p.y}`;
      assert(!used.get(id).has(key),`Reused port on ${id}`); used.get(id).add(key); sides.add(side);
      const perimeter=((p.x===n.x||p.x===n.x+width)&&p.y>n.y&&p.y<n.y+height)||((p.y===n.y||p.y===n.y+height)&&p.x>n.x&&p.x<n.x+width);
      assert(perimeter,`Port outside perimeter: ${id}`);
    }
    assert.equal(r.points[0].x,r.sourcePort.x); assert.equal(r.points[0].y,r.sourcePort.y);
    assert.equal(r.points.at(-1).x,r.targetPort.x); assert.equal(r.points.at(-1).y,r.targetPort.y);
  }
  assert.equal(sides.size,4,'All four component faces should be available to routing');
});

test('all map segments are orthogonal and stay out of every component interior', () => {
  for(const s of segments(result.routes)) {
    assert((s.a.x===s.b.x)!==(s.a.y===s.b.y),'Segment must be orthogonal and nonzero');
    for(const n of data.nodes) assert(!through(s,n),`Connector ${s.edge} enters ${n.id}`);
  }
});

test('the complete map has no shared collinear connector tracks', () => noSharedTracks(result.routes));

test('parallel segments with overlapping spans have at least eight units of separation', () => {
  const lines=segments(result.routes);
  for(let i=0;i<lines.length;i++) for(let j=0;j<i;j++) {
    const a=lines[i],b=lines[j];if(a.edge===b.edge)continue;
    if(a.a.y===a.b.y && b.a.y===b.b.y && Math.max(Math.min(a.a.x,a.b.x),Math.min(b.a.x,b.b.x))<Math.min(Math.max(a.a.x,a.b.x),Math.max(b.a.x,b.b.x)))
      assert(Math.abs(a.a.y-b.a.y)>=8);
    if(a.a.x===a.b.x && b.a.x===b.b.x && Math.max(Math.min(a.a.y,a.b.y),Math.min(b.a.y,b.b.y))<Math.min(Math.max(a.a.y,a.b.y),Math.max(b.a.y,b.b.y)))
      assert(Math.abs(a.a.x-b.a.x)>=8);
  }
});

test('an unobstructed neighboring pair keeps a direct connection', () => {
  const nodes=[{id:'a',x:60,y:120},{id:'b',x:540,y:120}];
  const r=api.routeConnections(nodes,[{source:'a',target:'b'}],width,height).routes[0];
  assert.equal(r.points.length,2);
});

test('a blocking component forces a detour rather than a line through its body', () => {
  const nodes=[{id:'a',x:60,y:120},{id:'obstacle',x:540,y:120},{id:'b',x:1020,y:120}];
  const r=api.routeConnections(nodes,[{source:'a',target:'b'}],width,height);
  assert(r.routes[0].points.length>=4);
  for(const s of segments(r.routes)) for(const n of nodes) assert(!through(s,n));
});

test('multiple forward and reverse connections receive separate deterministic routes', () => {
  const nodes=[{id:'a',x:60,y:120},{id:'b',x:540,y:120}];
  const edges=Array.from({length:7},(_,i)=>i<5?{source:'a',target:'b'}:{source:'b',target:'a'});
  const first=api.routeConnections(nodes,edges,width,height),second=api.routeConnections(nodes,edges,width,height);
  noSharedTracks(first.routes);
  assert.equal(JSON.stringify(first),JSON.stringify(second));
});
