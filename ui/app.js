import {newRun, applyEvent, failRun, VERDICTS, safeURL} from './race-state.js';
import {raceControls, liveHeaders} from './race-access.js';
import {proofMetrics, efficientFrontier} from './proof.js';

const $ = selector => document.querySelector(selector);
const elements = {}, states = {};
let firstEngine = 'tavily', active = false, replayPair = null, replaySource = null, controller = null;
let replayMode = false, autoplayDone = false, raceVisible = false, ready = false, liveAvailable = false;
let auditQuestion = '';
const origins = {};
const names = {single: 'Single engine', second_opinion: 'Backstop', telem_default: 'Telem default'};
const element = (tag, cls, text) => { const node = document.createElement(tag); if(cls) node.className = cls; if(text !== undefined) node.textContent = text; return node; };
const notify = text => { $('#race-message').textContent = text; };

function setActive(value) {
  active = value;
  const controls = raceControls({liveAvailable, active: value,
    hasKey: Boolean($('#api-key').value.trim()), replayAvailable: Boolean(replayPair)});
  $('#run-button').disabled = controls.disableRun;
  $('#run-button').hidden = !controls.showCredentials;
  $('#credentials').hidden = !controls.showCredentials;
  $('#replay-only').hidden = controls.showCredentials;
  $('#question').disabled = value;
  $('#question').readOnly = controls.readOnlyQuestion;
  $('#api-key').disabled = value;
  $('#replay-button').disabled = controls.disableReplay;
  $('#stop-button').hidden = !value;
}

function render(side) {
  const state = states[side], column = elements[side], find = selector => column.querySelector(selector);
  find('.telem-count').textContent = String(state.telemCalls);
  find('.cost-count').textContent = `$${state.cost.toFixed(4)}`;
  find('.time-count').replaceChildren(document.createTextNode((state.elapsed / 1000).toFixed(1)), element('span', '', 's'));
  find('.run-status').textContent = state.status;
  find('.session').textContent = state.session ? `Telem session · ${state.session}` : state.finished ? 'No Telem session recorded' : 'Session pending';
  const steps = find('.steps');
  if (state.steps.length) {
    steps.replaceChildren(...state.steps.map((step, i) => {
      const row = element('li'), title = element('div', 'step-query');
      title.append(element('span', 'step-num', String(i + 1).padStart(2, '0')), element('span', '', step.query));
      row.append(title);
      const chips = element('div', 'step-chips');
      step.engines.forEach(engine => chips.append(element('span', 'chip', engine)));
      if (step.verdict) chips.append(element('span', 'verdict', VERDICTS[step.verdict] || step.verdict));
      if (step.cached) chips.append(element('span', 'chip', 'Cached'));
      if (chips.childNodes.length) row.append(chips);
      if (step.why) row.append(element('p', 'step-note', step.why));
      return row;
    }));
    steps.scrollTop = steps.scrollHeight;
  } else steps.replaceChildren(element('li', 'empty', 'Waiting for the question.'));
  const declined = state.answer?.status === 'NOT_FOUND';
  find('.answer-label').textContent = state.failure ? 'Run failed' : declined ? 'Not found · a deliberate outcome' : 'Final answer';
  const fullAnswer = state.failure || state.answer?.final || (state.finished ? 'The run ended without a final answer.' : 'Following the evidence…');
  const lead = typeof Intl.Segmenter === 'function' ? [...new Intl.Segmenter('en', {granularity:'sentence'}).segment(fullAnswer)][0]?.segment.trim() : fullAnswer;
  find('.answer-text').textContent = lead || fullAnswer;
  find('.answer-full').textContent = fullAnswer;
  find('.answer-details').hidden = !state.answer;
  find('.sources').replaceChildren(...(state.answer?.citations || []).map((url, i) => {
    const safe = safeURL(url); if (!safe) return null;
    const link = element('a', '', `[${i + 1}] ${safe.hostname.replace(/^www\./, '')} ↗`);
    link.href = safe.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
  }).filter(Boolean));
  $('#export-audit').disabled = !states.left?.events.length && !states.right?.events.length;
}

function closeConnections() { replaySource?.close(); replaySource = null; controller?.abort(); controller = null; }
function reset(mode) {
  closeConnections(); replayMode = mode !== 'live';
  for (const [side, setup] of Object.entries({left:'single',right:'second_opinion'})) {
    states[side] = newRun(setup, firstEngine); states[side].status = 'Starting'; origins[side] = null; render(side);
  }
  $('#race-mode').textContent = mode === 'live' ? 'Live run' : mode === 'fixture' ? 'Synthetic fixture · not a live run' : 'Replay of a live run';
  setActive(true);
}
function receive(side, event) {
  if (!states[side]) throw new Error('Unrecognized race side.');
  if (applyEvent(states[side], event)) {
    origins[side] = performance.now() - states[side].elapsed; render(side);
  }
  if (states.left.finished && states.right.finished) finish();
}
function finish() { setActive(false); const interrupted = Object.values(states).some(state => Boolean(state.failure)); notify(interrupted ? 'One agent stopped before completion. The received audit trail is available below.' : replayMode ? (liveAvailable ? 'Recorded run complete. Use your API key to run a question above.' : 'Recorded run complete. Replay it or export the audit trail below.') : 'Both agents have finished. Export the audit trail below.'); }
function fail(message, side) {
  for (const name of side ? [side] : ['left', 'right']) {
    if (side || !states[name].finished) { failRun(states[name], message); render(name); }
  }
  notify(message);
  if (states.left.finished && states.right.finished) setActive(false);
}

for (const [side, setup] of Object.entries({left:'single',right:'second_opinion'})) {
  const column = $('#column-template').content.firstElementChild.cloneNode(true);
  column.id = `column-${side}`;
  if (side === 'right') column.classList.add('ours');
  column.querySelector('.column-tag').textContent = side === 'left' ? 'The first search' : 'The second opinion';
  column.querySelector('.column-name').textContent = names[setup];
  elements[side] = column; states[side] = newRun(setup, firstEngine); $('#columns').append(column);
}
render('left'); render('right');

setInterval(() => {
  if (!active || document.hidden) return;
  for (const side of ['left', 'right']) {
    if (!states[side].finished && origins[side] !== null) {
      states[side].elapsed = Math.max(states[side].elapsed, performance.now() - origins[side]);
      elements[side].querySelector('.time-count').replaceChildren(document.createTextNode((states[side].elapsed / 1000).toFixed(1)),element('span','','s'));
    }
  }
}, 100);

function playReplay() {
  if (!replayPair || active) return;
  autoplayDone = true;
  reset(replayPair.fixture ? 'fixture' : 'replay');
  $('#question').value = replayPair.question;
  auditQuestion = replayPair.question;
  notify(replayPair.fixture ? 'Synthetic test events. These are not product results.' : 'Recorded evidence, played at its original pace.');
  const source = new EventSource(`/replay/${encodeURIComponent(replayPair.name)}`);
  replaySource = source;
  source.addEventListener('log', message => { try { const payload = JSON.parse(message.data); receive(payload.side, payload.event); } catch { source.close(); fail('The recording contains an invalid event.'); } });
  source.addEventListener('complete', () => { source.close(); if (!(states.left.finished && states.right.finished)) fail('The recording ended before final totals.'); });
  source.addEventListener('stream_error', () => { source.close(); fail('The recorded stream was interrupted.'); });
  source.onerror = () => { source.close(); if (active) fail('The recording disconnected. You can replay it or run a question.'); };
}
$('#replay-button').addEventListener('click', playReplay);
$('#stop-button').addEventListener('click', () => { closeConnections(); fail('Stopped. The received events are still available to export.'); });

async function consumeStream(response) {
  if (!response.body) throw new Error('Streaming is unavailable in this browser.');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = '';
  while (true) {
    const {done,value} = await reader.read();
    if (done) break;
    pending += decoder.decode(value,{stream:true}).replace(/\r\n/g,'\n');
    let end;
    while ((end = pending.indexOf('\n\n')) >= 0) {
      const block = pending.slice(0,end); pending = pending.slice(end+2);
      const kind = block.split('\n').find(line=>line.startsWith('event:'))?.slice(6).trim();
      const raw = block.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
      if (!raw) continue;
      const payload = JSON.parse(raw);
      if (kind === 'log') receive(payload.side,payload.event);
      if (kind === 'stream_error') fail(payload.message || 'One agent stopped.',payload.side);
    }
  }
  if (!(states.left.finished && states.right.finished)) throw new Error('The connection ended before both agents recorded final totals.');
}
$('#api-key').addEventListener('input', () => setActive(active));
$('#race-form').addEventListener('submit', async event => {
  event.preventDefault(); if (active || !liveAvailable) return;
  const question = $('#question').value.trim(); if (!question) return;
  let headers;
  try { headers = liveHeaders($('#api-key').value); } catch (error) { notify(error.message); return; }
  auditQuestion = question;
  autoplayDone = true; reset('live'); notify('Both agents are searching. Every counter comes from the event log.');
  const abort = new AbortController(); controller = abort;
  try {
    const response = await fetch('/race-stream',{method:'POST',headers,body:JSON.stringify({question,left:'single',right:'second_opinion'}),signal:abort.signal});
    if (!response.ok) { const error = await response.json(); throw new Error(typeof error.detail === 'string' ? error.detail : error.detail?.message || 'The race could not start.'); }
    await consumeStream(response);
  } catch(error) { if (error.name !== 'AbortError') fail(error.message); }
});
$('#export-audit').addEventListener('click', () => {
  const data = {product:'Backstop',mode:$('#race-mode').textContent,question:auditQuestion,left:states.left.events,right:states.right.events};
  const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));
  const anchor = element('a'); anchor.href=url; anchor.download='backstop-race-audit.json';document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
});

function svgNode(tag, attributes={}, text) { const node=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attributes).forEach(([k,v])=>node.setAttribute(k,v));if(text!==undefined)node.textContent=text;return node; }
function drawFrontier(results) {
  const points=Object.entries(results.setups).filter(([key])=>names[key]).map(([key,value])=>({key,name:names[key],accuracy:value.accuracy,cost:value.cost_per_q_total}));
  const max=Math.max(...points.map(p=>p.cost),.001)*1.2, x=v=>85+v/max*790,y=v=>260-v*220;
  const svg=svgNode('svg',{viewBox:'0 0 1000 320',role:'img','aria-label':`Measured answer accuracy versus total cost per question. ${results.n_test} held-out questions per setup.`});
  for(let i=0;i<=4;i++) {const gy=y(i/4);svg.append(svgNode('line',{x1:85,x2:950,y1:gy,y2:gy,stroke:'#d0d0c4'}),svgNode('text',{x:67,y:gy+4,'text-anchor':'end'},`${i*25}%`),svgNode('text',{x:x(max*i/4),y:284,'text-anchor':'middle'},`$${(max*i/4).toFixed(3)}`));}
  const frontier=efficientFrontier(points);
  if(frontier.length>1) svg.append(svgNode('polyline',{points:frontier.map(p=>`${x(p.cost)},${y(p.accuracy)}`).join(' '),fill:'none',stroke:'#555c49','stroke-width':1.5}));
  points.forEach((p,i)=>{const circle=svgNode('circle',{cx:x(p.cost),cy:y(p.accuracy),r:p.key==='second_opinion'?7:5,fill:p.key==='second_opinion'?'#a3432c':'#696f5d'});circle.append(svgNode('title',{},`${p.name}: ${(p.accuracy*100).toFixed(1)}% accuracy, $${p.cost.toFixed(4)}, n=${results.n_test}`));svg.append(circle);const offset=p.key==='second_opinion'?-16:18+i*5;svg.append(svgNode('text',{x:x(p.cost)+12,y:y(p.accuracy)+offset,class:'chart-label'},p.name));});
  svg.append(svgNode('text',{x:510,y:313,'text-anchor':'middle'},'Total cost per question (search + page reads + model calls)'),svgNode('text',{transform:'translate(24 150) rotate(-90)','text-anchor':'middle'},'Answer accuracy'));
  $('#frontier-chart').replaceChildren(svg);$('#chart-note').textContent=`Observed frontier · n = ${results.n_test} per setup`;
}
function metric(selector, value, unit='') { const node=$(selector);node.replaceChildren(document.createTextNode(value));if(unit)node.append(element('small','',unit)); }
const emptyProof = $('.proof-numbers').innerHTML, emptyFrontier = $('#frontier-chart').innerHTML;
function renderProof(results) {
  const p=proofMetrics(results);
  if(!p){$('.proof-numbers').innerHTML=emptyProof;$('#frontier-chart').innerHTML=emptyFrontier;$('#chart-note').textContent='Measured points appear when the evaluation is complete.';return;}
  if(p.wrong===null){metric('#wrong-value',String(p.ours.wrong));$('#wrong-label').textContent='Wrong answers';}
  else {metric('#wrong-value',Math.abs(p.wrong).toFixed(1),'%');$('#wrong-label').textContent=p.wrong>=0?'Wrong answers cut':'More wrong answers';}
  $('#wrong-count').textContent=`${p.single.wrong} → ${p.ours.wrong} wrong · n = ${p.n} per setup`;
  metric('#accuracy-value',Math.abs(p.accuracy).toFixed(1),'pp');
  $('#accuracy-label').textContent=Math.abs(p.accuracy)<.005?'Accuracy tied with Telem default':p.accuracy>0?'Accuracy above Telem default':'Accuracy below Telem default';
  $('#accuracy-count').textContent=`${p.baseline.correct} → ${p.ours.correct} correct · n = ${p.n}`;
  if(p.cost!==null){metric('#cost-value',Math.abs(p.cost).toFixed(1),'%');$('#cost-label').textContent=p.cost>=0?'Lower total cost':'Higher total cost';}
  $('#cost-count').textContent=`$${p.baseline.cost_per_q_total.toFixed(4)} → $${p.ours.cost_per_q_total.toFixed(4)} · n = ${p.n}`;
  drawFrontier(results);
}
async function loadPresentation() {
  try {
    const response=await fetch('/presentation',{cache:'no-store'});if(!response.ok)throw new Error();const data=await response.json();
    firstEngine=data.first_engine||'tavily';liveAvailable=data.live_available===true;
    if(data.miss){metric('#miss-number',(data.miss.rate*100).toFixed(1),'%');$('#miss-count').textContent=`${data.miss.misses} / ${data.miss.n} held-out ${data.miss.engine} searches · answer absent from top five results`;}
    $('#proof-status').textContent=data.status;renderProof(data.results);if(!active)setActive(false);
  } catch {$('#proof-status').textContent='Results unavailable · reconnect to load the artifacts';}
}
function maybeAutoplay(){if(ready&&raceVisible&&!autoplayDone&&replayPair&&!replayPair.fixture)playReplay();}
new IntersectionObserver(entries=>{raceVisible=entries[0].isIntersecting;maybeAutoplay();},{threshold:.2}).observe($('#race'));
async function initialize(){
  await loadPresentation();
  try{const response=await fetch('/replays');if(!response.ok)throw new Error();const pairs=await response.json();replayPair=pairs.find(p=>!p.fixture&&p.name.includes('golden'))||pairs.find(p=>!p.fixture)||pairs[0]||null;
    if(replayPair){$('#question').value=replayPair.question;$('#race-mode').textContent=replayPair.fixture?'Synthetic fixture available':'Replay of a live run';}
    else{$('#race-mode').textContent=liveAvailable?'API key required for live races':'Replay-only mode';notify(liveAvailable?'Enter your API key and a question to run both agents.':'No recording is available yet. Live races are disabled on this server.');}
  }catch{$('#race-mode').textContent='Live server unavailable';notify('Connect the race server to run questions or replay a recording.');}
  ready=true;setActive(false);maybeAutoplay();
}
initialize();setInterval(()=>{if(!document.hidden)loadPresentation();},30000);

// The diagram is the only decorative motion. Tokens follow the actual branch paths.
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
let diagramVisible=false,diagramPaused=reduced.matches,frame=0,start=null;
const mobileParticles = Array.from({length: 5}, () => { const dot = element('i','mobile-particle'); dot.setAttribute('aria-hidden','true'); $('.diagram-mobile').append(dot); return dot; });
const tracks=[['query',0,1.4],['engine',1.4,2.8],['hit',2.8,4.7],['rescue',2.8,4.7],['stop',2.8,4.7]].map(([name,from,to])=>({dot:$(`.${name}-dot`),path:$(`#${name}-track`),from,to}));
function animateDiagram(now){
  if(!diagramVisible||diagramPaused){frame=0;return;}start??=now;const t=((now-start)/1000)%5.8;
  tracks.forEach(({dot,path,from,to})=>{const running=t>=from&&t<=to;dot.style.opacity=running?'1':'0';if(running){const point=path.getPointAtLength(path.getTotalLength()*(t-from)/(to-from));dot.setAttribute('cx',point.x);dot.setAttribute('cy',point.y);}});
  if (innerWidth <= 540) {
    const container = $('.diagram-mobile').getBoundingClientRect();
    const nodes = [$('.mobile-route > span'), ...document.querySelectorAll('.mobile-route strong'), ...document.querySelectorAll('.mobile-paths > div')].map(el => el.getBoundingClientRect());
    const routes = [[0,1],[1,2],[2,3],[2,4],[2,5]];
    tracks.forEach(({from,to},i) => { const dot=mobileParticles[i], running=t>=from&&t<=to;dot.style.opacity=running?'1':'0';if(running){const a=nodes[routes[i][0]],b=nodes[routes[i][1]],progress=(t-from)/(to-from);const ax=a.left+a.width/2-container.left,ay=a.bottom-container.top+4,bx=b.left+b.width/2-container.left,by=b.top-container.top-4;dot.style.left=`${ax+(bx-ax)*progress}px`;dot.style.top=`${ay+(by-ay)*progress}px`;} });
  }
  frame=requestAnimationFrame(animateDiagram);
}
function diagramButton(){ $('#diagram-play').textContent=diagramPaused?'Play diagram ▷':'Pause diagram Ⅱ';$('#diagram-play').setAttribute('aria-pressed',String(diagramPaused)); }
$('#diagram-play').addEventListener('click',()=>{diagramPaused=!diagramPaused;diagramButton();if(!diagramPaused&&!frame)frame=requestAnimationFrame(animateDiagram);});
new IntersectionObserver(entries=>{diagramVisible=entries[0].isIntersecting;if(diagramVisible&&!diagramPaused&&!frame)frame=requestAnimationFrame(animateDiagram);},{threshold:.2}).observe($('#mechanism'));
reduced.addEventListener('change',()=>{diagramPaused=reduced.matches;diagramButton();if(!diagramPaused&&!frame)frame=requestAnimationFrame(animateDiagram);});diagramButton();

// A fresh commit link keeps the pitch connected to the team's ongoing work.
fetch('https://api.github.com/repos/oliverrowebardeen/backstop/commits?sha=main&per_page=1',{signal:AbortSignal.timeout(5000)}).then(r=>r.ok?r.json():[]).then(([commit])=>{if(/^[a-f0-9]{40}$/.test(commit?.sha)){$('#build-link').textContent=`Latest commit ${commit.sha.slice(0,7)} ↗`;$('#build-link').href=`https://github.com/oliverrowebardeen/backstop/commit/${commit.sha}`;}}).catch(()=>{});
