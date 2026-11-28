const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {runInNewContext} = require('node:vm');
const source = readFileSync(resolve(__dirname, '../aa_industry_planner/static/aa_industry_planner/plan_controls.js'), 'utf8');

function filterButton(key, value, pressed = false) {
  return {
    dataset: {[key]: value},
    pressed: String(pressed),
    classList: {toggle() {}},
    addEventListener(event, callback) { this[event] = callback; },
    setAttribute(_, value) { this.pressed = value; },
    focus() {},
  };
}

test('each details panel saves its open or closed state in its own cookie', () => {
  const panels = ['aip_blueprints', 'aip_reaction_formulas'].map(panelCookie => ({
    dataset: {panelCookie}, open: false, addEventListener(event, callback) { this[event] = callback; },
  }));
  const writes = [];
  runInNewContext(source, {
    document: {
      querySelector: () => null, addEventListener() {},
      querySelectorAll: selector => selector === '[data-panel-cookie]' ? panels : [],
      set cookie(value) { writes.push(value); },
    },
    location: {protocol: 'https:'},
  });
  panels[0].open = true;
  panels[0].toggle();
  panels[1].toggle();
  assert.deepEqual(writes, [
    'aip_blueprints=open; Path=/; Max-Age=31536000; SameSite=Lax; Secure',
    'aip_reaction_formulas=closed; Path=/; Max-Age=31536000; SameSite=Lax; Secure',
  ]);
});

function browser({failSave=false, failCopy=false}={}) {
  const events={}, copied=[], requests=[];
  const feedback={}, status={}, details={open:false};
  const checkbox={checked:true,disabled:false,closest:selector=>selector==='form'?form:checkbox};
  const twin={checked:false,disabled:false};
  const save={hidden:false};
  const form={action:'/check/5',dataset:{itemId:'5'},querySelector:()=>feedback,addEventListener(){}};
  let reviewed=false;
  const card={classList:{toggle:(_,checked)=>{reviewed=checked;}}};
  const textarea={value:'Isogen 1200000\nZydrine 2',dataset:{},closest:()=>details,focus(){this.focused=true;},select(){this.selected=true;}};
  const rows = [['Isogen 1200000','minerals'],['Zydrine 2','minerals'],['Robotics 8','pi'],['Cadmium 15','moon-goo']].map(([buyLine,buyCategory],index)=>({
    dataset:{buyLine,buyCategory},classList:{contains:()=>index===0},
  }));
  textarea.value=rows.map(row=>row.dataset.buyLine).join('\n');
  const stocked = {dataset: {}, hidden: true};
  const [missingFilter, allFilter] = [filterButton('buyFilter', 'missing', true), filterButton('buyFilter', 'all')];
  const section={querySelector:selector=>selector==='[data-multibuy-text]'?textarea:status,querySelectorAll:selector=>selector==='[data-buy-row]'?[...rows, stocked]:rows};
  const filters = {closest: () => section, querySelectorAll: () => [missingFilter, allFilter], hidden: true};
  const buttons=['','unchecked','minerals','pi','moon-goo'].map(value=>({dataset:{copyMultibuy:value},closest:()=>section,addEventListener(_,fn){this.click=fn;}}));
  for (const [mode, copyText] of [
    ['gas', 'Amber Mykoserocin 50\nFullerite-C50 100'],
    ['compressed-gas', 'Compressed Amber Mykoserocin 53\nCompressed Fullerite-C50 106'],
  ]) buttons.push({dataset:{copyMultibuy:mode,copyText},closest:()=>section,addEventListener(_,fn){this.click=fn;}});
  runInNewContext(source,{
    document:{querySelector:()=>null,addEventListener:(event,fn)=>{events[event]=fn;},querySelectorAll:selector=>{
      if(selector==='[data-checklist-save]')return[save];
      if(selector==='[data-checklist]')return[form];
      if(selector==='[data-copy-multibuy]')return buttons;
      if(selector==='[data-buy-filter-controls]')return[filters];
      if(selector.includes('data-review-item'))return[card];
      if(selector.includes('input[name'))return[checkbox,twin];
      return[];
    }},
    FormData:class {constructor(){this.checked=checkbox.checked&&!checkbox.disabled;}},
    fetch:async (url,options)=>{requests.push({url,...options});return{ok:!failSave,json:async()=>({checked:options.body.checked})};},
    navigator:{clipboard:{writeText:async text=>{if(failCopy)throw Error('denied');copied.push(text);}}},
  });
  return {checkbox,twin,feedback,status,textarea,details,buttons,copied,requests,save,stocked,missingFilter,allFilter,get reviewed(){return reviewed;},change:()=>events.change({target:checkbox})};
}

test('checkbox sends its value before disabling and synchronizes every copy',async()=>{
  const ui=browser();
  await ui.change();
  assert.equal(ui.requests[0].body.checked,true);
  assert.equal(ui.twin.checked,true);
  assert.equal(ui.checkbox.disabled,false);
  assert.equal(ui.feedback.textContent,'Saved');
  assert.equal(ui.reviewed,true);
  assert.equal(ui.save.hidden,true);
});

test('failed reminder save restores controls without marking the item reviewed',async()=>{
  const ui=browser({failSave:true});
  await ui.change();
  assert.equal(ui.checkbox.checked,false);
  assert.equal(ui.twin.checked,false);
  assert.equal(ui.checkbox.disabled,false);
  assert.equal(ui.reviewed,false);
  assert.match(ui.feedback.textContent,/Could not save/);
});

test('copy all includes reviewed items and unchecked copy omits them',async()=>{
  const ui=browser();
  await ui.buttons[0].click();
  assert.equal(ui.copied[0],'Isogen 1200000\nZydrine 2\nRobotics 8\nCadmium 15');
  await ui.buttons[1].click();
  assert.equal(ui.copied[1],'Zydrine 2\nRobotics 8\nCadmium 15');
  assert.match(ui.status.textContent,/Copied 3 items/);
});

test('buy filters reveal stocked items while copying only shortages', async () => {
  const ui = browser();
  assert.equal(ui.stocked.hidden, true);
  ui.allFilter.click();
  assert.equal(ui.stocked.hidden, false);
  assert.equal(ui.allFilter.pressed, 'true');
  assert.equal(ui.missingFilter.pressed, 'false');
  await ui.buttons[0].click();
  assert.equal(ui.copied[0], 'Isogen 1200000\nZydrine 2\nRobotics 8\nCadmium 15');
  ui.missingFilter.click();
  assert.equal(ui.stocked.hidden, true);
});

test('category copy includes only lines in the selected category',async()=>{
  const ui=browser();
  for (const button of ui.buttons.slice(2,5)) await button.click();
  assert.deepEqual(ui.copied,['Isogen 1200000\nZydrine 2','Robotics 8','Cadmium 15']);
});

test('gas buttons copy the raw or converted gas list supplied by the server',async()=>{
  const ui=browser();
  for (const button of ui.buttons.slice(5)) await button.click();
  assert.deepEqual(ui.copied,[
    'Amber Mykoserocin 50\nFullerite-C50 100',
    'Compressed Amber Mykoserocin 53\nCompressed Fullerite-C50 106',
  ]);
  assert.match(ui.status.textContent,/Copied 2 items/);
});

test('clipboard denial exposes exactly the requested text and preserves full-list copying',async()=>{
  const ui=browser({failCopy:true});
  await ui.buttons[6].click();
  assert.equal(ui.textarea.value,'Compressed Amber Mykoserocin 53\nCompressed Fullerite-C50 106');
  assert.equal(ui.details.open,true);
  assert.equal(ui.textarea.selected,true);
  await ui.buttons[0].click();
  assert.equal(ui.textarea.value,'Isogen 1200000\nZydrine 2\nRobotics 8\nCadmium 15');
});

test('name buttons copy exact names and select the name when clipboard access fails', async () => {
  const name = {textContent: 'Shared "A & B" Reaction Formula'};
  const button = {addEventListener(event, callback) { this[event] = callback; }};
  const click = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  const status = {};
  const fields = {'[data-copy-name-text]': name, '[data-copy-name-button]': button, '[data-copy-name-status]': status};
  const control = {querySelector: selector => fields[selector]};
  const copied = [], selected = [];
  const navigator = {clipboard: {writeText: async text => { copied.push(text); }}};
  runInNewContext(source, {
    document: {
      querySelector: () => null,
      querySelectorAll: selector => selector === '[data-copy-name]' ? [control] : [],
      addEventListener() {},
    },
    navigator,
    window: {getSelection: () => ({selectAllChildren: element => selected.push(element)})},
  });
  await button.click(click);
  assert.equal(click.defaultPrevented, true);
  assert.deepEqual(copied, ['Shared "A & B" Reaction Formula']);
  assert.equal(status.textContent, 'Name copied.');
  assert.deepEqual(selected, []);

  delete navigator.clipboard;
  await button.click(click);
  assert.deepEqual(selected, [name]);
  assert.equal(status.textContent, 'Copy the selected name manually.');
});

test('skill buttons copy each full list and expose the exact same list if clipboard access fails', async () => {
  const controls = ['Industry 1', 'Capital Ship Construction 3\nIndustry 3', 'Reactions 4'].map(value => {
    const text = {value, hidden: true, focus() { this.focused = true; }, select() { this.selected = true; }};
    const button = {addEventListener(event, callback) { this[event] = callback; }};
    const status = {};
    const fields = {'[data-copy-skills-text]': text, '[data-copy-skills-button]': button, '[data-copy-skills-status]': status};
    return {button, text, status, querySelector: selector => fields[selector]};
  });
  const copied = [];
  const navigator = {clipboard: {writeText: async text => { copied.push(text); }}};
  runInNewContext(source, {
    document: {
      querySelector: () => null,
      querySelectorAll: selector => selector === '[data-copy-skills]' ? controls : [],
      addEventListener() {},
    },
    navigator,
  });
  for (const control of controls) {
    await control.button.click();
    assert.equal(control.status.textContent, 'Skills copied.');
    assert.equal(control.text.hidden, true);
  }
  assert.deepEqual(copied, ['Industry 1', 'Capital Ship Construction 3\nIndustry 3', 'Reactions 4']);

  delete navigator.clipboard;
  const {button, text, status} = controls[1];
  await button.click();
  assert.equal(text.value, 'Capital Ship Construction 3\nIndustry 3');
  assert.equal(text.hidden, false);
  assert.equal(text.focused, true);
  assert.equal(text.selected, true);
  assert.equal(status.textContent, 'Copy the selected skills manually.');
});

function filteredBrowser() {
  const events={}, windowEvents={};
  const buttons = ['all', 'remaining', 'ready', 'checked'].map(value => filterButton('stepFilter', value, value === 'all'));
  const counter={}, empty={}, controls={}, reset={addEventListener(_,fn){this.click=fn;}};
  const steps = [
    {id:'step-1',dataset:{progress:'0',state:'ready'}},
    {id:'step-2',dataset:{progress:'0',state:'active'}},
    {id:'step-3',dataset:{progress:'100',state:'complete'}},
    {id:'step-4',dataset:{progress:'0',state:'blocked'}},
  ];
  const reviewed = new Set(['step-3','step-4']);
  steps.forEach(step=>{
    step.classList={contains:()=>reviewed.has(step.id),toggle:(_,value)=>value?reviewed.add(step.id):reviewed.delete(step.id)};
    step.contains=()=>false;
    step.scrollIntoView=()=>{step.scrolled=true;};
    step.matches=selector=>selector==='[data-step-collapse]';
  });
  const sequence={querySelectorAll:selector=>selector==='[data-step-filter]'?buttons:steps,querySelector:selector=>({
    '[data-step-filter-count]':counter,'[data-step-filter-empty]':empty,
    '[data-step-filter-controls]':controls,'[data-step-filter-reset]':reset,
  })[selector]};
  const feedback={};
  const form={action:'/check/4',dataset:{itemId:'4'},querySelector:()=>feedback};
  const checkbox={checked:false,closest:selector=>selector==='form'?form:checkbox};
  const location={origin:'https://example.test',pathname:'/industry/plans/1/',search:'',hash:'',href:'https://example.test/industry/plans/1/'};
  runInNewContext(source,{
    document:{querySelector:()=>sequence,querySelectorAll:selector=>selector.includes('input[name')?[checkbox]:selector.includes('data-review-item')?[steps[3]]:[],addEventListener:(name,fn)=>{(events[name]??=[]).push(fn);}},
    window:{addEventListener:(name,fn)=>{windowEvents[name]=fn;}},
    location,URL,
    FormData:class{},fetch:async()=>({ok:true,json:async()=>({checked:checkbox.checked})}),
  });
  return {steps,counter,empty,controls,reset,checkbox,location,
    activeFilter(){return buttons.find(button => button.pressed === 'true').dataset.stepFilter;},
    filter(value){buttons.find(button => button.dataset.stepFilter === value).click();},
    visible(){return steps.filter(step=>!step.hidden).map(step=>step.id);},
    changeReminder:()=>Promise.all(events.change.map(fn=>fn({target:checkbox}))),
    follow(hash){events.click.forEach(fn=>fn({target:{closest:()=>({href:location.href+hash})}}));},
    hash(hash){location.hash=hash;windowEvents.hashchange();},
  };
}

test('step filters distinguish unfinished output, ready inputs and checked reminders',()=>{
  const ui=filteredBrowser();
  assert.deepEqual(ui.visible(),['step-1','step-2','step-3','step-4']);
  assert.equal(ui.controls.hidden,false);
  ui.filter('remaining');
  assert.deepEqual(ui.visible(),['step-1','step-2','step-4']);
  ui.filter('ready');
  assert.deepEqual(ui.visible(),['step-1']);
  assert.equal(ui.counter.textContent,'1 of 4 steps · inputs first');
  ui.filter('checked');
  assert.deepEqual(ui.visible(),['step-3','step-4']);
  assert.equal(ui.activeFilter(), 'checked');
});

test('unchecking the last reviewed step updates filtered results and exposes a reset',async()=>{
  const ui=filteredBrowser();
  ui.steps[2].classList.toggle('is-reviewed',false);
  ui.filter('checked');
  assert.deepEqual(ui.visible(),['step-4']);
  await ui.changeReminder();
  assert.deepEqual(ui.visible(),[]);
  assert.equal(ui.empty.hidden,false);
  assert.equal(ui.counter.textContent,'0 of 4 steps · inputs first');
  ui.reset.click();
  assert.equal(ui.activeFilter(),'all');
  assert.equal(ui.visible().length,4);
  assert.equal(ui.empty.hidden,true);
});

test('links and hash navigation reveal steps hidden by the current filter',()=>{
  const ui=filteredBrowser();
  ui.filter('ready');
  ui.follow('#step-2');
  assert.equal(ui.activeFilter(),'all');
  assert.equal(ui.steps[1].hidden,false);
  assert.equal(ui.steps[1].open,true);
  ui.filter('checked');
  ui.hash('#step-1');
  assert.equal(ui.activeFilter(),'all');
  assert.equal(ui.steps[0].hidden,false);
  assert.equal(ui.steps[0].scrolled,true);
});

test('fee details support focus and Escape without expanding the production step',()=>{
  const events={}, triggerEvents={}, boxEvents={}, classes=new Set();
  const trigger={addEventListener:(name,fn)=>{triggerEvents[name]=fn;},getBoundingClientRect:()=>({left:850,right:900,bottom:600})};
  const popup={style:{},offsetWidth:500,offsetHeight:600};
  const box={querySelector:selector=>selector==='button'?trigger:popup,addEventListener:(name,fn)=>{boxEvents[name]=fn;},contains:target=>target===trigger,classList:{add:value=>classes.add(value),remove:value=>classes.delete(value)}};
  runInNewContext(source,{
    document:{querySelector:()=>null,querySelectorAll:selector=>selector==='[data-fee]'?[box]:[],addEventListener:(name,fn)=>{(events[name]??=[]).push(fn);}},
    window:{innerWidth:1000,innerHeight:800,addEventListener(){}},
  });
  let prevented=0,stopped=0;
  triggerEvents.click({preventDefault(){prevented++;},stopPropagation(){stopped++;}});
  assert.equal(prevented,1); assert.equal(stopped,1);
  assert.equal(popup.style.left,'350px'); assert.equal(popup.style.top,'188px');
  boxEvents.keydown({key:'Escape',preventDefault(){},stopPropagation(){}});
  assert.equal(classes.has('is-fee-dismissed'),true);
  triggerEvents.focus();
  assert.equal(classes.has('is-fee-dismissed'),false);
  events.click.forEach(fn=>fn({target:{}}));
  assert.equal(classes.has('is-fee-dismissed'),true);
  boxEvents.mouseenter();
  assert.equal(classes.has('is-fee-dismissed'),false);
});
