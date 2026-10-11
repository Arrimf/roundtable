// Мини-DOM для прогона JS страницы. НАЗНАЧЕНИЕ УЗКОЕ, и его пределы
// названы честно (обе ревизии потребовали не переоценивать защиту):
// ловит — ReferenceError/TypeError синхронного пути (висячие ссылки,
//   обращения к удалённым переменным) при инициализации и applyVoices;
// НЕ ловит — опечатки в id/селекторах (getElementById создаёт любой
//   узел, querySelector всегда отвечает), асинхронные ветки (fetch не
//   резолвится, EventSource/observer немы), реальную отрисовку и
//   обработчики. Это сетка от одного класса дефектов, не браузер.
function mkNode(tag){ const n={
  tagName:(tag||'div').toUpperCase(), children:[], dataset:{},
  style:{setProperty(){},removeProperty(){}}, title:'', textContent:'',
  value:'', checked:false, hidden:false, className:'',
  attrs:{},
  classList:{add(){},remove(){},toggle(){},contains:()=>false},
  setAttribute(k,v){this.attrs[k]=String(v)},
  getAttribute(k){return k in this.attrs?this.attrs[k]:(k==='title'?(this.title||null):null)},
  removeAttribute(k){delete this.attrs[k];if(k==='title')this.title=''},
  appendChild(c){this.children.push(c);return c},
  contains:()=>false, closest(){return null},
  addEventListener(){}, removeEventListener(){}, setPointerCapture(){},
  querySelector(){return mkNode('div')},
  querySelectorAll(){return {forEach(){}}},
  set innerHTML(v){this._html=v}, get innerHTML(){return this._html||''},
}; return n;}
const ids={};
global.document={
  getElementById(id){ if(!ids[id])ids[id]=mkNode('div'); ids[id].id=id; return ids[id]; },
  createElement:t=>mkNode(t),
  addEventListener(){},
  querySelectorAll(){return {forEach(){}}},
  body:mkNode('body'),
  documentElement:mkNode('html'),
};
global.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
global.matchMedia=()=>({matches:false});
global.addEventListener=()=>{};
global.innerWidth=1600; global.innerHeight=900;
global.EventSource=class{constructor(){} set onmessage(f){this._f=f}};
global.MutationObserver=class{constructor(){} observe(){}};
global.AudioContext=class{constructor(){this.state='running';this.currentTime=0;this.destination={}}
  resume(){} createOscillator(){return{type:'',frequency:{value:0},connect(){},start(){},stop(){}}}
  createGain(){return{gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}}}};
global.fetch=()=>new Promise(()=>{});
global.confirm=()=>false; global.alert=()=>{};
global.window=global;
