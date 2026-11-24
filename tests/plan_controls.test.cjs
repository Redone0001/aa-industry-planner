const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {runInNewContext} = require('node:vm');
const source = readFileSync(resolve(__dirname, '../aa_industry_planner/static/aa_industry_planner/plan_controls.js'), 'utf8');

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
  const row={cells:[{textContent:'Isogen'}],querySelector:()=>checkbox};
  const section={querySelector:selector=>selector==='[data-multibuy-text]'?textarea:status,querySelectorAll:()=>[row]};
  const buttons=['','unchecked'].map(value=>({dataset:{copyMultibuy:value},closest:()=>section,addEventListener(_,fn){this.click=fn;}}));
  runInNewContext(source,{
    document:{addEventListener:(event,fn)=>{events[event]=fn;},querySelectorAll:selector=>{
      if(selector==='[data-checklist-save]')return[save];
      if(selector==='[data-checklist]')return[form];
      if(selector==='[data-copy-multibuy]')return buttons;
      if(selector.includes('data-review-item'))return[card];
      return[checkbox,twin];
    }},
    FormData:class {constructor(){this.checked=checkbox.checked&&!checkbox.disabled;}},
    fetch:async (url,options)=>{requests.push({url,...options});return{ok:!failSave,json:async()=>({checked:options.body.checked})};},
    navigator:{clipboard:{writeText:async text=>{if(failCopy)throw Error('denied');copied.push(text);}}},
  });
  return {checkbox,twin,feedback,status,textarea,details,buttons,copied,requests,save,get reviewed(){return reviewed;},change:()=>events.change({target:checkbox})};
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
  assert.equal(ui.copied[0],'Isogen 1200000\nZydrine 2');
  await ui.buttons[1].click();
  assert.equal(ui.copied[1],'Zydrine 2');
  assert.match(ui.status.textContent,/Copied 1 item/);
});

test('clipboard denial exposes exactly the requested text and preserves full-list copying',async()=>{
  const ui=browser({failCopy:true});
  await ui.buttons[1].click();
  assert.equal(ui.textarea.value,'Zydrine 2');
  assert.equal(ui.details.open,true);
  assert.equal(ui.textarea.selected,true);
  await ui.buttons[0].click();
  assert.equal(ui.textarea.value,'Isogen 1200000\nZydrine 2');
});
