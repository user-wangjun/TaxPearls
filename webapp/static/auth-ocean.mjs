/* Full-screen TaxPearls ocean. Directional wind waves, horizontal crest
 * compression, Fresnel sky reflections and crest foam share one live surface. */
import * as THREE from './vendor/three.module.mjs';

export function mountOcean(canvas) {
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
const state={time:0,frames:0,paused:reduced.matches,wave:1,speed:1,glow:1,ready:false,lost:false,fps:0,active:true};
let renderer;try{renderer=new THREE.WebGLRenderer({canvas,antialias:true,powerPreference:'high-performance'});}catch(e){throw e;}
renderer.setPixelRatio(Math.min(devicePixelRatio,1.25,Math.sqrt(1500000/(innerWidth*innerHeight))));renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.NoToneMapping;
const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(44,innerWidth/innerHeight,.1,500);
camera.position.set(0,3.4,12.8);camera.lookAt(0,4.65,-6.5);
let waveSeed=0x73d29;const waveRandom=()=>{waveSeed=(Math.imul(waveSeed,1664525)+1013904223)>>>0;return waveSeed/4294967296;};
const swellData=[],swellMotion=[];
for(let i=0;i<48;i++){
 const a=waveRandom(),b=waveRandom(),phase=waveRandom()*Math.PI*2;
 // Broad swells carry the flow; crossed middle wavelengths give the nearby
 // water full, rounded crests instead of a nearly flat long-wave surface.
 const long=i<8,middle=i>=8&&i<24,wind=i>=24&&i<40;
 const band=long?0:middle?Math.floor((i-8)/8):wind?Math.floor((i-24)/8):0;
 const angle=(long?.55:middle?.60:.12)+(a-.5)*(long?.60:middle?1.8:wind?1.45:2.0);
 const k0=long?.22+.24*b:middle?.54*1.75**band*(.78+.44*b):wind?1.9*1.75**band*(.8+.4*b):6.8*(.85+.3*b);
 const kx=Math.round(Math.cos(angle)*k0*160/(2*Math.PI))*2*Math.PI/160,kz=Math.round(Math.sin(angle)*k0*160/(2*Math.PI))*2*Math.PI/160,k=Math.hypot(kx,kz);
 const amplitude=(long?.075:middle?.090*.70**band:wind?.026*.65**band:.006)*(.80+.40*a);
 swellData.push(new THREE.Vector4(kx,kz,amplitude,phase));swellMotion.push(new THREE.Vector2(.75*Math.sqrt(9.81*k+.000074*k*k*k),long?.82:middle?.85:wind?.65:.45));
}
function currentTravel(time){return {x:.34*time+.40*(1-Math.cos(time*.15)),z:.12*Math.sin(time*.10)};}
function waveYaw(x,z,time,travel){let yaw=0;for(let i=0;i<8;i++){const w=swellData[i],phase=(x-travel.x)*w.x+(z-travel.z)*w.y-time*swellMotion[i].x+w.w;yaw+=w.z*state.wave*Math.sin(phase)*w.y*.55;}return Math.max(-.07,Math.min(.07,yaw));}
const sheetBounds=Array.from({length:6},()=>new THREE.Vector4()),sheetYawFade=Array.from({length:6},()=>new THREE.Vector2());
const shared={uSurfaceNear:{value:null},uSurfaceFar:{value:null},uCurrentTravel:{value:new THREE.Vector2()},uSwells:{value:swellData},uSwellMotion:{value:swellMotion},uSheetPose:{value:null},uSheetBounds:{value:sheetBounds},uSheetYawFade:{value:sheetYawFade},uTime:{value:0},uWave:{value:1},uCamera:{value:camera.position},uSunDirection:{value:new THREE.Vector3()},uClearDirection:{value:new THREE.Vector3()},uSkyIrradiance:{value:new THREE.Vector3()},uGlow:{value:1},uResolution:{value:new THREE.Vector2()},uSky:{value:null},uSkyStamp:{value:0},uWaveNear:{value:null},uWaveFar:{value:null},uChopNear:{value:null},uChopFar:{value:null},uInvProjection:{value:camera.projectionMatrixInverse},uCameraWorld:{value:camera.matrixWorld}};
// One photometric source, pre-exposed before storing in RGB16F.
// RGB single-scattering atmosphere is an approximation, not measured weather.
const solarModel={normalIlluminanceLux:100000,angularRadius:.00465,preExposure:1/50000,rayleigh:[5.8e-6,13.5e-6,33.1e-6],mie:2e-5,rayleighHeight:8000,mieHeight:1200,earthRadius:6360000,atmosphereHeight:80000};
const solarSolidAngle=2*Math.PI*(1-Math.cos(solarModel.angularRadius));
function airMass(cosine){const c=Math.max(0,Math.min(1,cosine)),zenith=Math.acos(c)*180/Math.PI;return 1/(c+.50572*Math.pow(96.07995-zenith,-1.6364));}
function atmosphereTransmission(cosine,height=0){const m=airMass(cosine);return solarModel.rayleigh.map(beta=>Math.exp(-m*(beta*8000*Math.exp(-height/8000)+solarModel.mie*1200*Math.exp(-height/1200))));}
function clearSkyRadiance(direction,sun){
 const R=solarModel.earthRadius,r=R+3,top=R+solarModel.atmosphereHeight,dy=direction[1];
 const far=-r*dy+Math.sqrt(r*r*dy*dy+top*top-r*r),cosine=direction.reduce((a,x,i)=>a+x*sun[i],0);
 const phaseR=3*(1+cosine*cosine)/(16*Math.PI),g=.76,phaseM=(1-g*g)/(4*Math.PI*Math.pow(1+g*g-2*g*cosine,1.5));
 const optical=[0,0,0],radiance=[0,0,0];
 for(let i=0;i<12;i++){const a=(i/12)**2*far,b=((i+1)/12)**2*far,t=(a+b)/2,step=b-a;
  const altitude=Math.sqrt(r*r+2*r*dy*t+t*t)-R,dR=Math.exp(-altitude/8000),dM=Math.exp(-altitude/1200);
  const localSunY=(sun[0]*direction[0]*t+sun[1]*(r+dy*t)+sun[2]*direction[2]*t)/(R+altitude),T=atmosphereTransmission(localSunY,altitude).map(x=>localSunY>=-Math.sqrt(2*altitude/R)?x:0);
  for(let c=0;c<3;c++){const extinction=(solarModel.rayleigh[c]*dR+solarModel.mie*dM)*step;
   radiance[c]+=Math.exp(-optical[c]-extinction*.5)*T[c]*(solarModel.rayleigh[c]*dR*phaseR+solarModel.mie*dM*phaseM)*step;
   optical[c]+=extinction;}}
 // Isotropic second-order scattering closure. It redistributes source light
 // according to wavelength-dependent optical depth, not a separate sky tint.
 return radiance.map((x,c)=>{const R=solarModel.rayleigh[c]*8000,M=solarModel.mie*1200,tau=R+M;
  const multiple=R/tau*(1-Math.exp(-tau*airMass(dy)))*(1-Math.exp(-tau*airMass(sun[1])))/(4*Math.PI);
  return (x+multiple)*solarModel.normalIlluminanceLux*solarModel.preExposure*state.glow;});
}
function updateSkyIrradiance(){const sun=shared.uSunDirection.value.toArray(),irradiance=[0,0,0];
 for(let i=0;i<4;i++)for(let j=0;j<8;j++){const y=(i+.5)/4,az=(j+.5)*Math.PI/4,xz=Math.sqrt(1-y*y),d=[Math.sin(az)*xz,y,Math.cos(az)*xz];
  const sky=clearSkyRadiance(d,sun);for(let c=0;c<3;c++)irradiance[c]+=sky[c]*y*2*Math.PI/32;}
 shared.uSkyIrradiance.value.fromArray(irradiance);
}
const noiseCode=`float hash(vec2 p){vec3 q=fract(vec3(p.xyx)*.1031);q+=dot(q,q.yzx+33.33);return fract((q.x+q.y)*q.z);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+1.),f.x),f.y);}
float fbm(vec2 p){float f=0.,a=.5;for(int i=0;i<4;i++){f+=a*noise(p);p=mat2(.8,-.6,.6,.8)*p*2.03+7.1;a*=.5;}return f;}`;
const analyticWaveCode=`uniform vec4 uSwells[48];uniform vec2 uSwellMotion[48];uniform vec2 uCurrentTravel;
void sampleSurface(vec2 p,float footprint,out vec3 f,out vec2 displacement,out mat2 j){f=vec3(0.);displacement=vec2(0.);j=mat2(1.);
 for(int i=0;i<48;i++){vec4 wave=uSwells[i];float k=length(wave.xy),weight=1.-smoothstep(.5,2.5,k*footprint),a=wave.z*uWave*weight;
  float phase=dot(p-uCurrentTravel,wave.xy)-uTime*uSwellMotion[i].x+wave.w,sn=sin(phase),cs=cos(phase);vec2 d=wave.xy/k;
  f.x+=a*(sn+.07*sn*cs);f.yz+=a*(cs+.07*(cs*cs-sn*sn))*wave.xy;
  float chopGain=uSwellMotion[i].y*a;displacement-=chopGain*cs*d;
  j+=chopGain*sn*k*mat2(d.x*d.x,d.x*d.y,d.x*d.y,d.y*d.y);
 }}
`;
const waveCode=`uniform sampler2D uSurfaceNear,uSurfaceFar,uChopNear,uChopFar;uniform vec2 uCurrentTravel;
vec4 cachedWave(vec2 p){float blend=smoothstep(10.,15.,max(abs(p.x),abs(p.y)));return mix(texture2D(uSurfaceNear,p/32.+.5),texture2D(uSurfaceFar,p/160.+.5),blend);}
vec3 field(vec2 p,int count,float footprint){return cachedWave(p).xyz;}
vec2 chop(vec2 p){float blend=smoothstep(10.,15.,max(abs(p.x),abs(p.y)));
 return mix(texture2D(uChopNear,p/32.+.5).xy,texture2D(uChopFar,p/160.+.5).xy,blend);}
vec3 pointAt(vec2 p){vec2 displaced=p+chop(p);return vec3(displaced.x,field(p,20,0.).x,displaced.y);}
`;
const cacheScene=new THREE.Scene(),cacheCamera=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
const cacheQuad=new THREE.Mesh(new THREE.PlaneGeometry(2,2));cacheScene.add(cacheQuad);
const cacheVertex=`varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}`;
const waveTargets=[32,160].map(span=>({span,target:new THREE.WebGLRenderTarget(span===32?512:256,span===32?512:256,{count:2,type:THREE.HalfFloatType,depthBuffer:false})}));
const fieldCacheMaterial=new THREE.ShaderMaterial({uniforms:{...shared,uSpan:{value:32},uCacheSize:{value:512}},glslVersion:THREE.GLSL3,depthTest:false,depthWrite:false,vertexShader:cacheVertex,
 fragmentShader:`uniform float uTime,uWave,uSpan,uCacheSize;varying vec2 vUv;layout(location=0) out vec4 fieldOutput;layout(location=1) out vec4 chopOutput;${analyticWaveCode}
 void main(){vec2 p=(vUv-.5)*uSpan,displacement;vec3 f;mat2 j;sampleSurface(p,uSpan/uCacheSize,f,displacement,j);
 float det=max(.3,j[0][0]*j[1][1]-j[0][1]*j[1][0]);vec2 g=vec2(j[1][1]*f.y-j[0][1]*f.z,j[0][0]*f.z-j[1][0]*f.y)/det;
 fieldOutput=vec4(f.x,g,det);chopOutput=vec4(displacement,0.,1.);}`});
for(const layer of waveTargets)for(const texture of layer.target.textures){texture.wrapS=texture.wrapT=THREE.RepeatWrapping;texture.minFilter=THREE.LinearMipmapLinearFilter;texture.generateMipmaps=true;}
shared.uWaveNear.value=waveTargets[0].target.textures[0];shared.uWaveFar.value=waveTargets[1].target.textures[0];
shared.uChopNear.value=waveTargets[0].target.textures[1];shared.uChopFar.value=waveTargets[1].target.textures[1];
// Fit the floating support once per frame from five surrounding water samples.
// A six-pixel GPU target avoids repeating the fit in every ocean fragment.
const sheetPoseTarget=new THREE.WebGLRenderTarget(6,1,{type:THREE.HalfFloatType,depthBuffer:false});sheetPoseTarget.texture.minFilter=sheetPoseTarget.texture.magFilter=THREE.NearestFilter;shared.uSheetPose.value=sheetPoseTarget.texture;
const sheetPoseMaterial=new THREE.ShaderMaterial({uniforms:shared,depthTest:false,depthWrite:false,vertexShader:cacheVertex,
 fragmentShader:`uniform sampler2D uWaveNear,uWaveFar;uniform vec4 uSheetBounds[6];uniform vec2 uSheetYawFade[6];varying vec2 vUv;
 float heightAt(vec2 p){float blend=smoothstep(10.,15.,max(abs(p.x),abs(p.y)));return mix(textureLod(uWaveNear,p/32.+.5,0.).x,textureLod(uWaveFar,p/160.+.5,0.).x,blend);}
 void main(){int index=int(floor(gl_FragCoord.x));vec4 sheet=vec4(0.);float yaw=0.;for(int i=0;i<6;i++)if(i==index){sheet=uSheetBounds[i];yaw=uSheetYawFade[i].x;}
 vec2 halfSize=vec2(sheet.z*.5,sheet.w*.675);mat2 rotation=mat2(cos(yaw),-sin(yaw),sin(yaw),cos(yaw));
 float a=heightAt(sheet.xy+rotation*vec2(-halfSize.x,-halfSize.y)),b=heightAt(sheet.xy+rotation*vec2(halfSize.x,-halfSize.y));
 float c=heightAt(sheet.xy+rotation*vec2(-halfSize.x,halfSize.y)),d=heightAt(sheet.xy+rotation*vec2(halfSize.x,halfSize.y));
 float h=heightAt(sheet.xy)*.20+(a+b+c+d)*.20;
 vec2 slope=rotation*vec2((b+d-a-c)/max(sheet.z*2.,.01),(c+d-a-b)/max(sheet.w*2.7,.01));
 gl_FragColor=vec4(h,clamp(slope,vec2(-.08),vec2(.08)),1.);}`});
function updateSheetPose(){cacheQuad.material=sheetPoseMaterial;renderer.setRenderTarget(sheetPoseTarget);renderer.render(cacheScene,cacheCamera);}
// The water stays continuous under every sheet. Floating poses never reshape it.
shared.uSurfaceNear.value=waveTargets[0].target.textures[0];
shared.uSurfaceFar.value=waveTargets[1].target.textures[0];

let waveCacheTime=NaN,waveCacheGain=NaN;
function updateWaveCache(){if(waveCacheTime===state.time&&waveCacheGain===state.wave)return;
 for(const layer of waveTargets){cacheQuad.material=fieldCacheMaterial;fieldCacheMaterial.uniforms.uSpan.value=layer.span;fieldCacheMaterial.uniforms.uCacheSize.value=layer.target.width;renderer.setRenderTarget(layer.target);renderer.render(cacheScene,cacheCamera);}
 waveCacheTime=state.time;waveCacheGain=state.wave;
}
const lightCode=`vec3 sunDirection(){return uSunDirection;}
float cloudVisibility(vec3 p){
 // Cloud alpha is direct-beam transmittance. Diffuse skylight is handled
 // separately; a fixed sunlight floor would illuminate cloud-shadowed water.
 vec3 l=sunDirection();vec3 ray=normalize(l+vec3(p.x,0.,p.z)*max(l.y,0.)/1000.);
 return clamp(skySample(ray).a,0.,1.);
}`;
// Deterministic 3D value/cellular noise, generated locally. No image assets.
const noiseVoxels=new Uint8Array(64*64*64*2),features=new Float32Array(8*8*8*3);
let featureSeed=0x4f1bbcd9;
for(let i=0;i<features.length;i++){featureSeed^=featureSeed<<13;featureSeed^=featureSeed>>>17;featureSeed^=featureSeed<<5;features[i]=(featureSeed>>>0)/4294967296;}
let noiseSeed=0x9e3779b9;
for(let z=0;z<64;z++)for(let y=0;y<64;y++)for(let x=0;x<64;x++){
 const index=((z*64+y)*64+x)*2;noiseSeed^=noiseSeed<<13;noiseSeed^=noiseSeed>>>17;noiseSeed^=noiseSeed<<5;noiseVoxels[index]=noiseSeed>>>24;
 const cx=x>>3,cy=y>>3,cz=z>>3,fx=x/8-cx,fy=y/8-cy,fz=z/8-cz;let distance=3.;
 for(let dz=-1;dz<=1;dz++)for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++){
  const point=((((cz+dz)&7)*8+((cy+dy)&7))*8+((cx+dx)&7))*3;
  const px=dx+features[point]-fx,py=dy+features[point+1]-fy,pz=dz+features[point+2]-fz;
  distance=Math.min(distance,px*px+py*py+pz*pz);
 }noiseVoxels[index+1]=Math.round(Math.max(0,1.-Math.sqrt(distance))*255);
}
const cloudNoise=new THREE.Data3DTexture(noiseVoxels,64,64,64);cloudNoise.format=THREE.RGFormat;
cloudNoise.minFilter=cloudNoise.magFilter=THREE.LinearFilter;cloudNoise.wrapS=cloudNoise.wrapT=cloudNoise.wrapR=THREE.RepeatWrapping;cloudNoise.unpackAlignment=1;cloudNoise.needsUpdate=true;
// Quadratic elevation mapping allocates more pixels to the visible horizon.
const skyWidth=4096,skyHeight=1024,skyStrip=16;
const skyTarget=new THREE.WebGLRenderTarget(skyWidth,skyHeight,{type:THREE.HalfFloatType,depthBuffer:false}),skyPending=skyTarget.clone();
for(const target of [skyTarget,skyPending]){target.texture.wrapS=THREE.RepeatWrapping;target.texture.minFilter=THREE.LinearMipmapLinearFilter;target.texture.generateMipmaps=true;}
shared.uSky.value=skyTarget.texture;
// Cloud volume/weather/cirrus port from the user-selected 8025 preview.
// Atmospheric radiometry and exposure now use the common solar model.
// Reference sky with a thinner distant bank, continuous wind and natural sun occlusion.
// Adapters: Three.js output/uniforms, world-Z handedness, panorama coordinates,
// and cloud transmittance in alpha for consistent sun/water occlusion.
const solarLightingCode=`
const float solarRadius=${solarModel.angularRadius},solarOmega=${solarSolidAngle.toPrecision(12)};
const float sourceLux=${solarModel.normalIlluminanceLux.toFixed(1)},preExposure=${solarModel.preExposure};
const vec3 rayleighBeta=vec3(${solarModel.rayleigh.join(',')});
const float mieBeta=${solarModel.mie},rayleighHeight=${solarModel.rayleighHeight.toFixed(1)},mieHeight=${solarModel.mieHeight.toFixed(1)};
uniform vec3 uSkyIrradiance;
float opticalAirMass(float cosine){float c=clamp(cosine,0.,1.),zenith=acos(c)*57.2957795;return 1./(c+.50572*pow(96.07995-zenith,-1.6364));}
vec3 atmosphereTransmission(float cosine,float height){float m=opticalAirMass(cosine);
 return exp(-m*(rayleighBeta*rayleighHeight*exp(-max(height,0.)/rayleighHeight)+vec3(mieBeta*mieHeight)*exp(-max(height,0.)/mieHeight)));}
vec3 solarColor(){return atmosphereTransmission(uSunDirection.y,0.);}
vec3 solarIrradiance(float height){float visible=step(-sqrt(max(0.,2.*height/6360000.)),uSunDirection.y);
 return vec3(sourceLux*preExposure*uGlow*visible)*atmosphereTransmission(uSunDirection.y,height);}
vec3 solarRadiance(){return solarIrradiance(0.)/solarOmega;}
vec3 atmosphereRadiance(vec3 d,vec3 sun){
 float earth=6360000.,r=earth+3.,top=earth+80000.;
 float far=-r*d.y+sqrt(r*r*d.y*d.y+top*top-r*r),cosine=dot(d,sun);
 float phaseR=3.*(1.+cosine*cosine)/(16.*3.14159265),g=.76;
 float phaseM=(1.-g*g)/(12.566370614*pow(1.+g*g-2.*g*cosine,1.5));
 vec3 optical=vec3(0.),radiance=vec3(0.);
 for(int i=0;i<12;i++){float a=pow(float(i)/12.,2.)*far,b=pow(float(i+1)/12.,2.)*far,t=(a+b)*.5,stepSize=b-a;
  float altitude=sqrt(r*r+2.*r*d.y*t+t*t)-earth,dR=exp(-altitude/8000.),dM=exp(-altitude/1200.);
  float localSunY=dot(sun,vec3(0.,r,0.)+d*t)/(earth+altitude);
  vec3 T=atmosphereTransmission(localSunY,altitude)*step(-sqrt(max(0.,2.*altitude/earth)),localSunY),extinction=(rayleighBeta*dR+vec3(mieBeta)*dM)*stepSize;
  radiance+=exp(-optical-extinction*.5)*T*(rayleighBeta*dR*phaseR+vec3(mieBeta)*dM*phaseM)*stepSize;
  optical+=extinction;
 }
 vec3 tau=rayleighBeta*rayleighHeight+vec3(mieBeta*mieHeight);
 vec3 rayleighAlbedo=rayleighBeta*rayleighHeight/tau;
 vec3 multiple=rayleighAlbedo*(1.-exp(-tau*opticalAirMass(d.y)))*(1.-exp(-tau*opticalAirMass(sun.y)))/12.566370614;
 return (radiance+multiple)*(sourceLux*preExposure*uGlow);
}
`;
const referenceSolarCode=`${solarLightingCode}
    // Effective RGB extinction; scattering stays below total extinction.
    vec3 waterExtinction(){return vec3(.20,.06,.04);}
    vec3 waterScattering(){return vec3(.024,.034,.040);}
    // Reference-fitted surface concentration. Total extinction stays constant;
    // local absorption is extinction minus local scattering, never negative.
    // This is an effective depth profile, not measured water chemistry.
    float waterScatteringDensity(float depth){return .2+.8*exp(-max(depth,0.)/4.);}
    vec3 waterScatteringAtDepth(float depth){return waterScattering()*waterScatteringDensity(depth);}
    vec3 skyPhotonDirection(int index){return index==0?vec3(0,1,0):normalize(vec3(index==1?-.8:.8,.65,index==1?.3:-.3));}
    float waterPhase(float cosine,float g){
      float denominator=max(.0001,1.+g*g-2.*g*cosine);
      return (1.-g*g)/(12.566370614*denominator*sqrt(denominator));
    }`;
const skyCacheMaterial=new THREE.ShaderMaterial({uniforms:{...shared,noiseVolume:{value:cloudNoise}},depthTest:false,depthWrite:false,vertexShader:cacheVertex,
 fragmentShader:`
    precision highp sampler3D;varying vec2 vUv;

    uniform sampler3D noiseVolume;
    uniform float uTime,uGlow;uniform vec3 uSunDirection;
#define time uTime
${solarLightingCode}
    // Effective RGB extinction; scattering stays below total extinction.
    vec3 waterExtinction(){return vec3(.20,.06,.04);}
    vec3 waterScattering(){return vec3(.024,.034,.040);}
    // Reference-fitted surface concentration. Total extinction stays constant;
    // local absorption is extinction minus local scattering, never negative.
    // This is an effective depth profile, not measured water chemistry.
    float waterScatteringDensity(float depth){return .2+.8*exp(-max(depth,0.)/4.);}
    vec3 waterScatteringAtDepth(float depth){return waterScattering()*waterScatteringDensity(depth);}
    vec3 skyPhotonDirection(int index){return index==0?vec3(0,1,0):normalize(vec3(index==1?-.8:.8,.65,index==1?.3:-.3));}
    float waterPhase(float cosine,float g){
      float denominator=max(.0001,1.+g*g-2.*g*cosine);
      return (1.-g*g)/(12.566370614*denominator*sqrt(denominator));
    }
    float hash(vec2 p){vec3 q=fract(vec3(p.xyx)*.1031);q+=dot(q,q.yzx+33.33);return fract((q.x+q.y)*q.z);}
    float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),mix(hash(i+vec2(0,1)),hash(i+1.),f.x),f.y);}
    float fbm(vec2 p){float f=0.,a=.5;for(int i=0;i<5;i++){f+=a*noise(p);p=mat2(.8,-.6,.6,.8)*p*2.03+7.1;a*=.5;}return f;}

    float noise3(vec3 p){
      vec3 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
      return texture(noiseVolume,(i+f+.5)/64.).r;
    }
    float cellular(vec3 p){return texture(noiseVolume,(p+.5)/64.).g;}
    // Normalized two-lobe approximation for cloud scattering. The incoming
    // direction and view ray are expressed toward the sky, hence this cosine.
    float cloudPhase(float cosine){return .85*waterPhase(cosine,.6)+.15*waterPhase(cosine,-.2);}
    // Same source spectrum; less atmospheric column above the cloud layer.
    vec3 cloudSolar(){return solarIrradiance(1000.);}
    float density(vec3 p){
      if(p.y<800.||p.y>1280.)return 0.;
      // Advect the whole weather, bank and erosion field together.
      p.x-=time*28.;p.z+=time*5.;
      float profile=smoothstep(800.,890.,p.y)*(1.-smoothstep(1050.,1280.,p.y));
      // Separate kilometer-scale weather from the smaller cloud structure.
      // Anisotropy stretches the layer along its prevailing wind direction.
      vec2 weatherPosition=p.xz*.00032+vec2(1.8,5.2);
      float weather=noise3(vec3(weatherPosition.x,2.1,weatherPosition.y));
      float coverage=smoothstep(.43,.70,weather);
      vec3 q=p*vec3(.0011,.004,.002)+vec3(0.,2.7,5.8);float f=0.,a=.5;
      for(int i=0;i<4;i++){f+=a*noise3(q);q=q*2.07+vec3(1.1,7.3,2.4);a*=.5;}
      float erosion=noise3(p*vec3(.009,.015,.014)+vec3(4.8,2.3,1.7));
      float layer=max(f-.48-(1.-coverage)*.11-(1.-erosion)*.025,0.);
      // A broken bank of low clouds gives the sunset a composed silhouette.
      // Its density still has depth, illuminated edges and internal shadows.
      vec3 bank=(p-vec3(-1300.,1010.,5300.))/vec3(1300.,110.,620.);
      float lobes=smoothstep(.22,.62,cellular(p*vec3(.075,.085,.065)+vec3(3.2,6.7,1.9)));
      float body=max(1.-dot(bank,bank),0.);
      float cloudBank=max(body*(.12+.88*lobes)-(1.-erosion)*.3-.08,0.)*.9;
      float fineErosion=noise3(p*vec3(.027,.041,.036)+vec3(8.4,2.3,5.2));
      float brokenLayer=max(layer-(1.-fineErosion)*.018,0.);
      return (brokenLayer+cloudBank*.60)*profile*.65;
    }
    float cirrus(vec3 d){
      if(d.y<.025)return 0.;
      // Optical depth through a thin, high cloud layer. The wind-aligned
      // domain creates filaments rather than round low-altitude puffs.
      vec2 p=(d*(3200./d.y)).xz;
      p+=vec2(-90.,16.)*time;
      p=mat2(.94,-.342,.342,.94)*p;
      vec2 q=p*vec2(.00045,.0015)+vec2(0.,8.6);
      float cloudCoverage=smoothstep(.4,.68,fbm(p*.00031+vec2(5.2,8.6)));
      float warp=fbm(p*.0004+3.7);
      float strands=fbm(q+vec2(warp*2.5,warp*4.));
      float feather=fbm(q*2.4+vec2(2.1,7.3));
      float fine=fbm(q*5.7+vec2(8.4,2.3));
      float density=smoothstep(.45,.61,strands)*smoothstep(.34,.63,feather)*smoothstep(.25,.55,fine)*cloudCoverage;
      return density*smoothstep(.025,.12,d.y);
    }
    void main(){
      vec2 uv=vUv;
      float az=(uv.x-.5)*6.2831853,el=uv.y*uv.y*1.57079633;
      vec3 d=vec3(sin(az)*cos(el),sin(el),-cos(az)*cos(el));
      vec3 s=vec3(uSunDirection.x,uSunDirection.y,-uSunDirection.z);
      float y=max(d.y,0.);
      vec3 sky=atmosphereRadiance(d,s);
      float wisps=cirrus(d)*.58,highOpacity=1.-exp(-wisps*1.8);
      vec3 highCloud=uSkyIrradiance/3.14159265*.85+solarIrradiance(3200.)*cloudPhase(dot(d,s));
      sky=mix(sky,highCloud,highOpacity);
      vec3 cloud=vec3(0);float transmission=1.;
      if(d.y>.018){
        // Directional source is constant along this cloud ray. Compute it once;
        // solar-side edges brighten while other directions keep cool fill.
        vec3 directSource=cloudSolar()*cloudPhase(dot(d,s));
        float start=800./d.y,stepSize=480./d.y/64.;
        for(int i=0;i<64;i++){
          vec3 p=d*(start+(float(i)+.5)*stepSize);
          float rho=density(p),opacity=1.-exp(-rho*stepSize*.012);
          float optical=density(p+s*55.)*.66+density(p+s*170.)*1.38+density(p+s*380.)*2.52;
          float sunlight=exp(-optical*3.2);
          float ambient=.65+.35*smoothstep(850.,1250.,p.y);
          vec3 radiance=uSkyIrradiance/3.14159265*ambient+directSource*sunlight;
          radiance+=uSkyIrradiance/3.14159265*.20*(1.-sunlight);
          cloud+=radiance*opacity*transmission;transmission*=1.-opacity;
          if(transmission<.01)break;
        }
      }
      vec3 c=mix(sky,cloud+sky*transmission,smoothstep(.018,.06,y));
      gl_FragColor=vec4(c,mix(1.,transmission,smoothstep(.018,.06,y))*exp(-wisps*1.8));
    }`});
let skyCacheTime=NaN,skyCacheSun=new THREE.Vector3(),skyCacheGlow=NaN,skyRows=skyHeight,skyFront=skyTarget,skyBack=skyPending,skyBuildTime=0;
function updateSkyCache(){
 const changed=!skyCacheSun.equals(shared.uSunDirection.value)||skyCacheGlow!==state.glow;
 if(!Number.isFinite(skyCacheTime)||changed){updateSkyIrradiance();cacheQuad.material=skyCacheMaterial;renderer.setRenderTarget(skyFront);renderer.render(cacheScene,cacheCamera);shared.uSky.value=skyFront.texture;skyCacheTime=state.time;shared.uSkyStamp.value=state.time;skyCacheSun.copy(shared.uSunDirection.value);skyCacheGlow=state.glow;skyRows=skyHeight;return;}
 if(skyRows>=skyHeight){if(Math.abs(state.time-skyCacheTime)<.7)return;skyRows=0;skyBuildTime=state.time;skyBack.texture.generateMipmaps=false;}
 skyBack.texture.generateMipmaps=skyRows+skyStrip>=skyHeight;
 // RenderTarget scissors are framebuffer pixels. renderer.setScissor would
 // multiply by the display pixel ratio, leaving skipped rows at fractional DPR.
 cacheQuad.material=skyCacheMaterial;skyBack.scissor.set(0,skyRows,skyWidth,Math.min(skyStrip,skyHeight-skyRows));skyBack.scissorTest=true;renderer.setRenderTarget(skyBack);
 const current=shared.uTime.value;shared.uTime.value=skyBuildTime;renderer.render(cacheScene,cacheCamera);shared.uTime.value=current;skyBack.scissorTest=false;skyRows+=skyStrip;
 if(skyRows>=skyHeight){[skyFront,skyBack]=[skyBack,skyFront];shared.uSky.value=skyFront.texture;skyCacheTime=skyBuildTime;shared.uSkyStamp.value=skyBuildTime;}
}
const skyCode=`uniform sampler2D uSky;uniform vec3 uSunDirection;uniform float uSkyStamp;
vec4 skySample(vec3 d){
 // World-wind reprojection between cached sky updates keeps drift continuous.
 d=normalize(d+vec3(-28.,0.,-5.)*(uTime-uSkyStamp)*max(d.y,0.)/1000.);vec2 uv=vec2(atan(d.x,d.z)/6.2831853+.5,sqrt(asin(clamp(d.y,0.,1.))/1.57079633));vec2 dx=dFdx(uv),dy=dFdy(uv);dx.x-=floor(dx.x+.5);dy.x-=floor(dy.x+.5);return textureGrad(uSky,uv,dx,dy);}
vec3 daylightSky(vec3 d){return skySample(d).rgb;}
${referenceSolarCode}
vec3 referenceSun(vec3 d){
 float cosine=dot(d,uSunDirection),angle=cosine>0.?asin(clamp(length(cross(d,uSunDirection)),0.,1.)):3.14159265;
 // Pixel-integrated disc with common angular size and irradiance. The
 // normalized limb law has unit mean radiance over the entire solar disc.
 float pixelRadius=max(.00015,fwidth(angle)*.75);
 float disc=1.-smoothstep(solarRadius-pixelRadius,solarRadius+pixelRadius,angle);
 float limb=(.6+.4*sqrt(max(0.,1.-pow(angle/solarRadius,2.))))/.866666667;
 // Small energy-normalized lens-scatter approximation; its spectrum and
 // energy come from the same sun, with no independent colored aureole.
 float sigma=.012,scatteredFraction=.001;
 vec3 halo=solarIrradiance(0.)*scatteredFraction*exp(-angle*angle/(2.*sigma*sigma))/(6.2831853*sigma*sigma);
 return solarRadiance()*(1.-scatteredFraction)*disc*limb+halo;
}
vec3 skyBackdrop(vec2 uv){vec4 ray=uInvProjection*vec4(uv*2.-1.,1.,1.);vec3 d=normalize(mat3(uCameraWorld)*ray.xyz);
 vec4 sky=skySample(d);
 // The same moving cloud transmittance attenuates the disc and aureole.
 return sky.rgb+referenceSun(d)*sky.a;}

`;
const backdrop=new THREE.Mesh(new THREE.PlaneGeometry(2,2),new THREE.ShaderMaterial({uniforms:shared,depthTest:false,depthWrite:false,
vertexShader:`varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position.xy,1.,1.);}`,
fragmentShader:`uniform float uTime,uGlow;uniform mat4 uInvProjection,uCameraWorld;varying vec2 vUv;${noiseCode}${skyCode}void main(){gl_FragColor=vec4(skyBackdrop(vUv),1.);}`}));backdrop.renderOrder=-10;scene.add(backdrop);
// A projected grid keeps triangle sizes small in the actual view. The former
// world-space grid stretched across distant glints and produced visible facets.
const gridX=360,gridY=240;
const waterGeometry=new THREE.PlaneGeometry(2,2,gridX,gridY),positions=waterGeometry.attributes.position;
function updateOceanGrid(){
 camera.updateMatrixWorld();const m=camera.matrixWorld.elements,tan=Math.tan(camera.fov*Math.PI/360);
 const horizon=m[9]/(tan*m[5])-.012;
 for(let row=0;row<=gridY;row++)for(let column=0;column<=gridX;column++){
  const i=row*(gridX+1)+column,x=(column/gridX*2-1)*1.50,y=horizon+(-1.60-horizon)*row/gridY;
  const dx=-m[8]+x*tan*camera.aspect*m[0]+y*tan*m[4];
  const dy=-m[9]+x*tan*camera.aspect*m[1]+y*tan*m[5];
  const dz=-m[10]+x*tan*camera.aspect*m[2]+y*tan*m[6];
  const travel=Math.min(440,-camera.position.y/Math.min(dy,-.0001));
  positions.setXYZ(i,camera.position.x+dx*travel,0,camera.position.z+dz*travel);
 }positions.needsUpdate=true;
}
updateOceanGrid();
const water=new THREE.Mesh(waterGeometry,new THREE.ShaderMaterial({uniforms:shared,side:THREE.DoubleSide,
vertexShader:`uniform float uTime,uWave;varying vec3 vWorld;varying vec2 vBase;${waveCode}
void main(){vBase=position.xz;vWorld=pointAt(vBase);vWorld.y*=1.-smoothstep(180.,420.,length(vBase));gl_Position=projectionMatrix*modelViewMatrix*vec4(vWorld,1.);}`,
fragmentShader:`uniform vec3 uCamera;uniform vec2 uResolution;uniform mat4 uInvProjection,uCameraWorld;uniform float uTime,uWave,uGlow;varying vec3 vWorld;varying vec2 vBase;${noiseCode}${waveCode}${skyCode}${lightCode}
vec3 reflectionRadiance(vec3 d){
 // Rough microfacets looking below the horizon see water, not a bright
 // horizon pixel repeated around every crest.
 vec3 bounce=uSkyIrradiance*waterScattering()/waterExtinction()/12.566370614;
 return mix(bounce,daylightSky(d),smoothstep(0.,.04,d.y));
}
vec3 environment(vec3 d,float spread){
 vec3 tangent=normalize(cross(d,abs(d.y)>.95?vec3(1.,0.,0.):vec3(0.,1.,0.))),bitangent=cross(tangent,d);
 vec3 c=reflectionRadiance(d)*.4;
 c+=(reflectionRadiance(normalize(d+tangent*spread))+reflectionRadiance(normalize(d-tangent*spread))+
 reflectionRadiance(normalize(d+bitangent*spread))+reflectionRadiance(normalize(d-bitangent*spread)))*.15;
 return c;
}
// Ported GGX/Smith and dielectric Fresnel from the supplied reference.
const float roughness=.145,sunIntensity=1.;
float filteredNormalDerivativeVariance,filteredSlopeVariance;
float fresnel(float cosine,float n1,float n2){
 float c=clamp(cosine,0.,1.),eta=n1/n2,t=sqrt(max(0.,1.-eta*eta*(1.-c*c)));
 float rs=(n1*c-n2*t)/max(n1*c+n2*t,.0001),rp=(n2*c-n1*t)/max(n2*c+n1*t,.0001);
 return .5*(rs*rs+rp*rp);
}
float solarLobe(vec3 n,vec3 v,vec3 l){
 float nv=max(dot(n,v),.001),nl=max(dot(n,l),0.);if(nl<=0.)return 0.;
 vec3 h=normalize(l+v);float nh=max(dot(n,h),0.);
 // Variance from pixel-filtered wind ripples stays in the BRDF instead of
 // disappearing with their normals. Derivative filtering prevents fireflies.
 float a2=clamp(pow(roughness,4.)+filteredSlopeVariance*.5+filteredNormalDerivativeVariance*.20,.00045,.12);
 float denominator=nh*nh*(a2-1.)+1.;float D=a2/(3.14159265*denominator*denominator);
 float gv=2.*nv/(nv+sqrt(a2+(1.-a2)*nv*nv)),gl=2.*nl/(nl+sqrt(a2+(1.-a2)*nl*nl));
 return D*gv*gl*fresnel(dot(v,h),1.,1.333)/(4.*nv)*sunIntensity;
}
float solarSpecular(vec3 n,vec3 v){
 // Integrate the finite solar disc rather than treating it as a point source.
 vec3 l=sunDirection(),t=normalize(cross(l,abs(l.y)>.95?vec3(1.,0.,0.):vec3(0.,1.,0.))),b=cross(t,l);
 float radius=solarRadius*.84;
 return solarLobe(n,v,l)*.333333+
 (solarLobe(n,v,normalize(l+t*radius))+solarLobe(n,v,normalize(l-t*radius))+
 solarLobe(n,v,normalize(l+b*radius))+solarLobe(n,v,normalize(l-b*radius)))*.166667;
}
float waveShadow(vec2 p,float height){vec3 l=sunDirection();vec2 direction=normalize(l.xz);float blocked=0.;
 for(int i=0;i<6;i++){float d=i==0?.35:i==1?.8:i==2?1.8:i==3?3.8:i==4?7.5:14.;
  float rayHeight=height+d*l.y/max(length(l.xz),.001),penumbra=.012+d*solarRadius;
  blocked=max(blocked,smoothstep(-penumbra,penumbra,field(p+direction*d,12,0.).x-rayHeight));}
 return 1.-blocked;
}
// Nearby crests occlude low reflected sky rays. A small broad bounce fills
// the blocked directions, instead of pinning every reflection to the horizon.
float reflectedSkyVisibility(vec2 p,float height,vec3 ray){
 float horizon=0.;vec2 direction=normalize(ray.xz);
 for(int i=0;i<3;i++){float distance=i==0?.8:i==1?2.2:5.5;
  horizon=max(horizon,(field(p+direction*distance,16,0.).x-height)/distance);}
 float slope=ray.y/max(length(ray.xz),.001);
 return smoothstep(horizon-.015,horizon+.04,slope);
}
// A few liquid-bound gold paths. Their shading shares the displaced water
// fragment, so crests naturally occlude the paths behind them.
float goldPath(vec2 p,float lane){
 p-=uCurrentTravel;float z=-18.+lane*7.+(2.4+lane*.25)*sin(p.x*.16+lane*.8-uTime*.045)+.24*sin(p.x*.55+lane);
 float d=p.y-z;d+=.035*noise(vec2(p.x*.8,lane+uTime*.04));float dist=abs(d);
 float width=max(.009,fwidth(d)*.50);
 float core=1.-smoothstep(width*.35,width*1.25,dist);
 float halo=exp(-d*d*32.)*.045;
 float flow=.32+.68*pow(.5+.5*sin(p.x*.44-uTime*.70+lane*2.1),3.);
 float fragment=.23+.77*smoothstep(.24,.65,noise(vec2(p.x*.62-uTime*.18,lane*2.3)));
 return (core*.27+halo)*flow*fragment;
}
void main(){float distance=length(uCamera-vWorld);
 // A continuous optical footprint avoids the roughness jumps caused by
 // derivatives crossing triangle boundaries in the previous stretched mesh.
 float grazing=clamp(abs(uCamera.y-vWorld.y)/max(distance,.1),.10,1.);
 float footprint=max(.004,distance*.808/uResolution.y/grazing);
 vec4 wave=cachedWave(vBase);vec3 f=wave.xyz;float determinant=wave.w;
 vec2 gradient=f.yz;float unresolvedSlopeVariance=0.;
 // Unresolved wind ripples break broad mirrors into small glints. Their
 // directions and phases vary instead of producing a regular crosshatch.
 for(int i=0;i<8;i++){float t=float(i),r1=fract(sin(t*83.17+6.4)*23947.13),r2=fract(sin(t*47.13+8.7)*41739.27);
 float k=6.8*pow(1.52,t)*(.85+.3*r2),angle=.85+(r1-.5)*2.6;vec2 direction=vec2(cos(angle),sin(angle));
 float weight=1.-smoothstep(.4,2.2,k*footprint);
 float amplitude=.10*pow(.71,t)*uWave;
 gradient+=direction*amplitude*cos(dot(vBase-uCurrentTravel,direction)*k-uTime*sqrt(9.81*k+.000074*k*k*k)*.75+r2*6.2831853)*weight;
 unresolvedSlopeVariance+=.5*amplitude*amplitude*(1.-weight*weight);}


 vec3 n=normalize(vec3(-gradient.x,1.,-gradient.y)),v=normalize(uCamera-vWorld);
 float nv=max(dot(n,v),0.);vec3 r=reflect(-v,n);float F=fresnel(nv,1.,1.333);
 filteredNormalDerivativeVariance=dot(dFdx(n),dFdx(n))+dot(dFdy(n),dFdy(n));filteredSlopeVariance=unresolvedSlopeVariance;
 float spread=sqrt(pow(roughness,4.)+filteredSlopeVariance*.5+filteredNormalDerivativeVariance*.20);
 float cloudT=cloudVisibility(vWorld),visibility=waveShadow(vBase,f.x)*cloudT;
 // Effective water volume scattering under sky + the same incident sun.
 vec3 illumination=uSkyIrradiance+solarIrradiance(0.)*max(dot(n,sunDirection()),0.)*visibility;
 vec3 waterBody=waterScattering()/waterExtinction()*illumination/12.566370614;
 float skyVisibility=reflectedSkyVisibility(vBase,f.x,r)*smoothstep(0.,.03,r.y);
 vec3 reflected=mix(waterBody,environment(normalize(vec3(r.x,max(.001,r.y),r.z)),spread),skyVisibility);
 vec3 c=mix(waterBody,reflected,F);
 c+=solarIrradiance(0.)*solarSpecular(n,v)*visibility;
 // Short white crests follow compression and slope, broken into patches by foam.
 float compression=max(0.,1.-determinant);float steep=length(gradient);
 float foam=smoothstep(.55,.9,compression)*smoothstep(.65,1.,f.x)*smoothstep(.35,.78,fbm(vBase*3.+uTime*.07));
 foam+=smoothstep(1.2,1.8,steep)*smoothstep(.65,1.,f.x)*.10;
 c=mix(c,(uSkyIrradiance+solarIrradiance(0.)*max(dot(n,sunDirection()),0.)*visibility)/3.14159265,min(.65,foam));
 float gold=0.;for(int lane=0;lane<4;lane++)gold+=goldPath(vBase,float(lane));
 float crestLight=.4+.6*max(dot(n,sunDirection()),0.);
 c+=vec3(1.7,1.12,.45)*gold*crestLight*uGlow;
 c=mix(c,daylightSky(normalize(vec3(vWorld.x-uCamera.x,.005,vWorld.z-uCamera.z))),1.-exp(-distance*.0008));
 gl_FragColor=vec4(c,1.);
}`}));water.frustumCulled=false;scene.add(water);
// Tax records make up the ocean's visual texture: broad pages in front,
// overlapping source streams in the middle and many records near the horizon.
const sourceTypes=['账簿明细','增值税发票','纳税申报表','资金流水'];
function makeRecordTexture(kind,serial=kind*17+28){
 // Fewer rows with tall glyphs compensate the grazing surface perspective.
 // Double-resolution canvas avoids losing fine strokes before projection.
 const c=document.createElement('canvas');c.width=2048;c.height=1280;const g=c.getContext('2d');g.scale(2,2);
 const text=(value,x,y,font,color,vertical=1)=>{g.save();g.translate(x,y);g.scale(1,vertical);g.font=font;g.fillStyle=color;g.fillText(value,0,0);g.restore();};
 g.fillStyle='rgba(5,18,32,.99)';g.fillRect(18,18,988,604);
 g.strokeStyle='rgba(142,179,198,.55)';g.lineWidth=2;g.strokeRect(18,18,988,604);
 g.fillStyle='#d3b678';g.fillRect(52,52,8,101);
 text(sourceTypes[kind],86,153,'700 70px "Microsoft YaHei", sans-serif','#fff3d4',1.45);
 text(['GENERAL LEDGER','VAT INVOICE','TAX RETURN','CASH FLOW'][kind]+'  /  '+String(serial).padStart(3,'0'),86,189,'600 24px Consolas, monospace','#d7c495');
 g.strokeStyle='rgba(154,180,192,.35)';g.beginPath();g.moveTo(52,217);g.lineTo(972,217);g.stroke();
 const headers=[['摘要','借方','贷方'],['开票项目','金额','税额'],['申报项目','本期','累计'],['交易摘要','收入','支出']][kind];
 headers.forEach((t,i)=>text(t,[62,491,758][i],295,'700 42px "Microsoft YaHei", sans-serif','#e3eff7',1.45));
 const rows=[['业务收入','费用结转','往来核对','期末余额'],['服务项目','购进项目','凭证关联','开票汇总'],['收入合计','销项数据','进项数据','申报核验'],['收款记录','付款记录','账款匹配','资金余额']][kind];
 for(let row=0;row<2;row++){
  const entry=(row+(serial%2)*2)%4,y=419+row*163;
  text(rows[entry],62,y,'700 44px "Microsoft YaHei", sans-serif','#f0f5f8',1.85);
  text(['128,600.00','36,240.00','8,716.00','156,124.00'][(entry+kind)%4],490,y,'700 36px Consolas, monospace','#ffffff',2.1);
  text(['7,716.00','2,174.40','522.96','9,367.44'][(entry+kind)%4],755,y,'700 36px Consolas, monospace','#ffffff',2.1);
  g.strokeStyle='rgba(113,151,174,.28)';g.beginPath();g.moveTo(52,y+25);g.lineTo(972,y+25);g.stroke();
 }
 const texture=new THREE.CanvasTexture(c);texture.colorSpace=THREE.SRGBColorSpace;texture.anisotropy=Math.min(16,renderer.capabilities.getMaxAnisotropy());return texture;
}
const recordTextures=sourceTypes.map((_,kind)=>makeRecordTexture(kind));
const recordDefinitions=[
 [-3.8,1.0,4.5,0,-.10,.74],[2.7,-.3,4.2,1,.09,.72],[-.2,-7.7,3.6,2,-.06,.58],
 [-7.2,-8,3.4,3,.12,.50],[6.7,-9.5,3.6,0,-.09,.50],[-3.9,-16,3.3,1,.05,.42],
 [2.8,-18,3.4,3,.12,.42],[-10.5,-18,3.2,2,-.08,.34],[10.5,-23,3.4,1,.08,.34],
 [-6.7,-28,3.3,0,.10,.27],[.6,-31,3.5,2,-.08,.30],[6.8,-35,3.5,1,.10,.25],
 [-14,-34,3.5,3,-.06,.22],[-4,-43,3.8,1,.06,.20],[12,-43,4.0,2,-.10,.20],
 [-12,-52,4.0,0,.05,.16],[3,-53,4.3,3,-.08,.18],[17,-55,4.5,1,.10,.16],
 [-9.3,.7,4.0,3,.14,.60]
];
const recordMeshes=[];
for(const [x,z,width,kind,yaw,opacity] of recordDefinitions){
 const height=width*.625;
 const record=new THREE.Mesh(new THREE.PlaneGeometry(width,height,36,22),new THREE.ShaderMaterial({
 uniforms:{...shared,uSheetIndex:{value:[0,1,18,2,3,4].indexOf(recordMeshes.length)},uRecord:{value:recordTextures[kind]},uAnchor:{value:new THREE.Vector2(x,z)},uYaw:{value:yaw},uHeight:{value:height},uOpacity:{value:opacity},uSelectedRow:{value:[.345,.09,.345,.09][kind]},uSelected:{value:0},uGather:{value:0}},transparent:true,depthTest:false,depthWrite:false,side:THREE.DoubleSide,
 vertexShader:`uniform float uTime,uWave,uYaw,uHeight,uSheetIndex;uniform sampler2D uSheetPose;uniform vec4 uSheetBounds[6];uniform vec2 uAnchor;varying vec2 vUv;varying float vDistance,vContact;varying vec3 vWorld;${waveCode}
 void main(){vUv=uv;float a=uYaw;mat2 rot=mat2(cos(a),-sin(a),sin(a),cos(a));vec2 base=uAnchor+rot*vec2(position.x,-position.y*1.35-uHeight*.50);vec3 p=pointAt(base);float waterHeight=p.y;
 // Flexible margins stay in the water; the inner sheet retains a gentle
 // multi-point support pose, with a bounded gap rather than a raised platform.
 if(uSheetIndex>=0.){vec4 sheet=vec4(0.);for(int i=0;i<6;i++)if(float(i)==uSheetIndex)sheet=uSheetBounds[i];
  vec3 pose=texture2D(uSheetPose,vec2((uSheetIndex+.5)/6.,.5)).xyz;
  float supported=pose.x+dot(pose.yz,base-sheet.xy);
  vec2 margin=abs(uv*2.-1.);float flex=smoothstep(.55,.96,max(margin.x,margin.y));
  p.y+=clamp(supported-p.y,-.02,.035)*(1.-flex);
 }p.y+=.015;vContact=p.y-waterHeight;vec4 mv=modelViewMatrix*vec4(p,1.);vWorld=p;vDistance=-mv.z;gl_Position=projectionMatrix*mv;}`,
 fragmentShader:`uniform sampler2D uRecord;uniform float uTime,uGlow,uOpacity,uSelectedRow,uSelected,uGather;
 uniform mat4 uInvProjection,uCameraWorld;varying vec2 vUv;varying float vDistance,vContact;varying vec3 vWorld;
 ${skyCode}${lightCode}
 void main(){vec4 record=texture2D(uRecord,vUv,-.25);float gather=uGather;
 float row=1.-smoothstep(.004,.012,abs(vUv.y-(uSelectedRow-.04)));record.rgb+=vec3(.12,.07,.02)*row*gather*uSelected;
 vec3 normal=normalize(cross(dFdx(vWorld),dFdy(vWorld)));
 vec3 incident=uSkyIrradiance+solarIrradiance(0.)*abs(dot(normal,uSunDirection))*cloudVisibility(vWorld);
 // Schematic ink retains a readability floor in cloud shadow.
 // Surface illumination still supplies the varying warm/cool response.
 record.rgb*=vec3(.12)+incident/3.14159265*.60;
 float edge=smoothstep(0.,.035,min(min(vUv.x,1.-vUv.x),min(vUv.y,1.-vUv.y)));float fog=1.-smoothstep(35.,85.,vDistance);
 // Continuous wetting avoids triangle-depth speckles at the shared interface.
 float dry=smoothstep(-.015,.012,vContact);record.rgb*=mix(.78,1.,dry);
 gl_FragColor=vec4(record.rgb,record.a*min(1.,uOpacity*1.80)*edge*fog*mix(.35,1.,dry));
 }`

 }));record.frustumCulled=false;scene.add(record);recordMeshes.push({mesh:record,x,z,height,width,kind,yaw,opacity,scale:1,cycle:0,currentKind:kind,ownedTexture:null});
}
const materialLanes=[
 {z:.6,ids:[0,1,18]}, {z:-10.5,ids:[2,3,4]},
 {z:-27,ids:[5,6,7,8]}, {z:-53,ids:[9,10,11,12]},
 {z:-94,ids:[13,14,15,16,17]}
];
function fitRecords(){
 const narrow=innerWidth/innerHeight<1.2,phone=innerWidth<500;
 materialLanes.forEach((lane,laneId)=>{
  let maxWidth=0;
  for(const id of lane.ids){const record=recordMeshes[id];record.z=lane.z;record.lane=laneId;
   const scale=laneId<2?(phone?.50:narrow?.83:1):[1,1,.88,.72,.56][laneId];
   record.mesh.geometry.scale(scale/record.scale,scale/record.scale,1);record.scale=scale;
   record.mesh.material.uniforms.uHeight.value=record.height*scale;
   maxWidth=Math.max(maxWidth,record.width*scale);
  }
  const view=camera.matrixWorldInverse.elements,depth=-(view[6]+view[10]*lane.z+view[14]);
  const viewHalf=depth*Math.tan(camera.fov*Math.PI/360)*camera.aspect;
  lane.spacing=Math.max(maxWidth*1.55+1.15,(viewHalf+maxWidth*.85+1.)*2/lane.ids.length);
  lane.bound=lane.spacing*lane.ids.length*.5;
  lane.ids.forEach((id,phase)=>{const record=recordMeshes[id];
   record.laneBound=lane.bound;record.startX=(phase+.5)*lane.spacing-lane.bound;
  });
 });
}
function materialFlow(item,time){
 const view=camera.matrixWorldInverse.elements,depth=-(view[6]+view[10]*item.z+view[14]);
 const viewHalf=depth*Math.tan(camera.fov*Math.PI/360)*camera.aspect;
 const width=item.width*item.scale,bound=item.laneBound??(viewHalf+width*.65+.5),length=bound*2;
 const raw=item.startX+currentTravel(time).x*(item.drift/.34)+bound,cycle=Math.floor(raw/length),x=raw-cycle*length-bound;
 const smooth=(a,b,v)=>{const t=Math.max(0,Math.min(1,(v-a)/(b-a)));return t*t*(3-2*t);};
 const fade=smooth(-bound,-viewHalf+width*.2,x)*(1-smooth(viewHalf-width*.2,bound,x));
 return {x,cycle,fade,bound};
}
function updateMaterialFlow(){
 const travel=currentTravel(state.time);shared.uCurrentTravel.value.set(travel.x,travel.z);
 recordMeshes.forEach((record,index)=>{
  record.drift=.34;const flow=materialFlow(record,state.time),u=record.mesh.material.uniforms;
  if(flow.cycle!==record.cycle){
   const kind=((record.kind+flow.cycle)%4+4)%4;
   const texture=flow.cycle===0?recordTextures[kind]:makeRecordTexture(kind,100+index+flow.cycle*recordMeshes.length);
   u.uRecord.value=texture;record.ownedTexture?.dispose();record.ownedTexture=flow.cycle===0?null:texture;record.cycle=flow.cycle;record.currentKind=kind;
   u.uSelectedRow.value=[.345,.09,.345,.09][kind];
  }
  u.uAnchor.value.set(flow.x,record.z+travel.z);u.uYaw.value=record.yaw+waveYaw(flow.x,record.z,state.time,travel);u.uOpacity.value=record.opacity*flow.fade;
  record.flow=flow;
 });
 // Six near sheets carry legible data; distant pages remain ocean detail.
 [0,1,18,2,3,4].forEach((id,index)=>{const r=recordMeshes[id],u=r.mesh.material.uniforms,a=u.uYaw.value,offset=-u.uHeight.value*.50;
  sheetBounds[index].set(u.uAnchor.value.x+Math.sin(a)*offset,u.uAnchor.value.y+Math.cos(a)*offset,r.width*r.scale,u.uHeight.value);
  sheetYawFade[index].set(a,r.flow.fade);
 });
 for(const stream of streamMeshes){stream.drift=.34;const flow=materialFlow(stream,state.time);stream.mesh.material.uniforms.uAnchor.value.set(flow.x,stream.z+travel.z);stream.mesh.material.uniforms.uFade.value=flow.fade;}
 const smooth=(a,b,v)=>{const t=Math.max(0,Math.min(1,(v-a)/(b-a)));return t*t*(3-2*t);};
 for(const item of harvestInstances){
  const r=item.source,progress=(r.flow.x+r.flow.bound)/(2*r.flow.bound),u=item.uniforms;
  u.uProgress.value=progress;u.uFade.value=r.flow.fade;u.uRadiusScale.value=Math.sqrt(r.scale)*(r.z<-12?.66:1);
  const onset=u.uOnset.value;
  r.mesh.material.uniforms.uSelected.value=1;
  r.mesh.material.uniforms.uGather.value=smooth(onset-.04,onset+.05,progress)*(1-smooth(onset+.20,onset+.36,progress))*r.flow.fade;
 }

}
// Fine columns of source identifiers flow with the crests between the pages.
const streamCanvas=document.createElement('canvas');streamCanvas.width=1024;streamCanvas.height=512;const streamContext=streamCanvas.getContext('2d');
streamContext.font='24px Consolas, monospace';streamContext.fillStyle='#c3dae5';
for(let row=0;row<11;row++)streamContext.fillText(['VAT   028  /  128600.00   /   7716.00','LED   001  /  36240.00    /   MATCH','RET   016  /  INPUT       /   OUTPUT','FLOW  039  /  RECEIPT     /   PAYMENT'][row%4],16,44+row*43);
const streamTexture=new THREE.CanvasTexture(streamCanvas);streamTexture.colorSpace=THREE.SRGBColorSpace;const streamMeshes=[];
for(const [x,z,w] of [[-1.4,5,4.7],[5.9,1,4.8],[-8.5,-2,4.5],[.8,-15,5.6],[-13,-19,5.4],[12,-28,6.5],[-1,-39,7.]]){
 const stream=new THREE.Mesh(new THREE.PlaneGeometry(w,w*.8,28,20),new THREE.ShaderMaterial({uniforms:{...shared,uRecord:{value:streamTexture},uAnchor:{value:new THREE.Vector2(x,z)},uFade:{value:1}},transparent:true,depthWrite:false,side:THREE.DoubleSide,
 vertexShader:`uniform float uTime,uWave;uniform vec2 uAnchor;varying vec2 vUv;${waveCode}void main(){vUv=uv;vec2 base=uAnchor+vec2(position.x,-position.y);vec3 p=pointAt(base);p.y+=.06;gl_Position=projectionMatrix*modelViewMatrix*vec4(p,1.);}`,
 fragmentShader:`uniform sampler2D uRecord;uniform float uFade;varying vec2 vUv;void main(){vec4 c=texture2D(uRecord,vUv);float mask=smoothstep(0.,.12,vUv.y)*smoothstep(0.,.12,1.-vUv.y);gl_FragColor=vec4(c.rgb,c.a*.74*mask*uFade);}`}));stream.frustumCulled=false;scene.add(stream);streamMeshes.push({mesh:stream,x,z,width:w,scale:1,startX:x});
}
// Each source page owns its own pearl. Extraction happens once on entry;
// the pearl, its caption and its thread then drift out with that same page.
const markerDefinitions=[0,1,2,3,4,6,18],harvestInstances=[];
const harvestCode=`
float harvestVisibility(){return smoothstep(uOnset-.03,uOnset+.08,uProgress);}
vec2 harvestBase(){return uAnchor+vec2(uHeight*.94+.12,.30);}
vec3 harvestCentre(){vec3 p=pointAt(harvestBase());p.y+=.07*uRadiusScale;return p;}
`;
const pearlGeometry=new THREE.SphereGeometry(.19,40,28);
const pearlFragment=`uniform mat4 uInvProjection,uCameraWorld;uniform vec3 uCamera;uniform float uTime,uGlow,uFade;varying vec3 vWorld,vNormal;varying float vVisibility;
${skyCode}${lightCode}
void main(){if(vVisibility<.015||uFade<.001)discard;
 vec3 n=normalize(vNormal),v=normalize(uCamera-vWorld),l=uSunDirection,h=normalize(l+v);
 float nv=max(dot(n,v),0.),nl=max(dot(n,l),0.),transmission=cloudVisibility(vWorld);
 // Warm ivory nacre instead of the previous blue-grey underside.
 // Angle-dependent tint modulates reflected light; it is not self emission.
 float sheen=pow(1.-nv,1.5);
 vec3 albedo=vec3(.97,.93,.82)*(1.+.035*sheen*cos(vec3(0.,2.1,4.2)+nv*15.+n.y*2.));
 float back=max(dot(-n,l),0.);
 // Normalized forward scattering of the existing sunlight through nacre.
 // A sphere intercepts pi*r^2 and scatters over 4*pi*r^2; the 1/4
 // factor and normalized phase preserve the allocated incident energy.
 float g=.55,cosine=dot(-l,v);
 float phase=(1.-g*g)/pow(max(.01,1.+g*g-2.*g*cosine),1.5);
 float thickness=.65+1.1*nv;
 vec3 subsurface=exp(-vec3(.055,.065,.08)*thickness)*pow(back,.35)*phase*.25;
 vec3 incident=uSkyIrradiance+solarIrradiance(0.)*transmission*(.20*nl+.80*subsurface);
 vec3 body=albedo*incident/3.14159265;
 vec3 reflection=reflect(-v,n),r=normalize(vec3(reflection.x,max(.08,reflection.y),reflection.z));
 vec3 tangent=normalize(cross(r,abs(r.y)>.95?vec3(1.,0.,0.):vec3(0.,1.,0.))),bitangent=cross(tangent,r);
 vec3 reflected=daylightSky(r)*.50+
 (daylightSky(normalize(r+tangent*.10))+daylightSky(normalize(r-tangent*.10))+
 daylightSky(normalize(r+bitangent*.10))+daylightSky(normalize(r-bitangent*.10)))*.125;
 // Layered nacre reflects a broad ivory-tinted environment lobe.
 float dielectric=.62+.38*pow(1.-nv,5.);body=mix(body,reflected*vec3(.99,.95,.84),dielectric);
 float highlight=pow(max(dot(n,h),0.),64.)*66./(8.*3.14159265)*.16;
 body+=solarIrradiance(0.)*highlight*transmission;
 gl_FragColor=vec4(body,uFade);
}`;
const harvestUniforms=`uniform float uTime,uWave,uHeight,uProgress,uOnset,uRadiusScale,uFade;uniform vec2 uAnchor;`;
const threadPositions=[],threadSteps=[];
for(let step=0;step<64;step++)for(const end of [0,1]){threadPositions.push(0,0,0);threadSteps.push((step+end)/64);}
const threadGeometry=new THREE.BufferGeometry();threadGeometry.setAttribute('position',new THREE.Float32BufferAttribute(threadPositions,3));threadGeometry.setAttribute('aT',new THREE.Float32BufferAttribute(threadSteps,1));
const haloGeometry=new THREE.BufferGeometry();haloGeometry.setAttribute('position',new THREE.Float32BufferAttribute([0,0,0],3));
const clueCaption=document.createElement('canvas');clueCaption.width=320;clueCaption.height=96;const clueCtx=clueCaption.getContext('2d');
clueCtx.font='500 36px "Microsoft YaHei", sans-serif';clueCtx.fillStyle='#dcc18a';clueCtx.fillText('风险线索',74,57);
const clueTexture=new THREE.CanvasTexture(clueCaption);clueTexture.colorSpace=THREE.SRGBColorSpace;
for(const [order,sourceId] of markerDefinitions.entries()){
 const source=recordMeshes[sourceId],sourceUniforms=source.mesh.material.uniforms;
 const uniforms={...shared,uAnchor:sourceUniforms.uAnchor,uHeight:sourceUniforms.uHeight,uYaw:sourceUniforms.uYaw,uRow:sourceUniforms.uSelectedRow,uProgress:{value:0},uOnset:{value:.17+(order%3)*.025},uRadiusScale:{value:1},uFade:{value:0},uPixelRatio:{value:renderer.getPixelRatio()},uCaption:{value:clueTexture}};
 const pearl=new THREE.Mesh(pearlGeometry,new THREE.ShaderMaterial({uniforms,transparent:true,
 vertexShader:`${harvestUniforms}varying vec3 vWorld,vNormal;varying float vVisibility;${waveCode}${harvestCode}void main(){vVisibility=harvestVisibility();vNormal=normal;vWorld=harvestCentre()+position*vVisibility*uRadiusScale;gl_Position=projectionMatrix*viewMatrix*vec4(vWorld,1.);}`,fragmentShader:pearlFragment}));pearl.frustumCulled=false;scene.add(pearl);
 const thread=new THREE.LineSegments(threadGeometry,new THREE.ShaderMaterial({uniforms,transparent:true,depthWrite:false,blending:THREE.AdditiveBlending,
 vertexShader:`${harvestUniforms}uniform float uYaw,uRow;attribute float aT;varying float vT;${waveCode}${harvestCode}
 void main(){vT=aT;float yaw=uYaw;vec2 local=vec2(uHeight*.16,uHeight*(uRow-.5));mat2 rotation=mat2(cos(yaw),-sin(yaw),sin(yaw),cos(yaw));vec2 start=uAnchor+rotation*vec2(local.x,-local.y*1.35-uHeight*.50);vec2 base=mix(start,harvestBase(),aT);base.y+=sin(aT*3.14159)*.28;vec3 p=pointAt(base);p.y+=.048;gl_Position=projectionMatrix*viewMatrix*vec4(p,1.);}`,
 fragmentShader:`uniform float uTime,uGlow,uFade,uProgress,uOnset;varying float vT;void main(){float signal=smoothstep(uOnset-.05,uOnset+.03,uProgress);float pulse=pow(.5+.5*cos(vT*14.-uTime*2.4),10.);gl_FragColor=vec4(vec3(.36,.22,.065)*(.10+.75*pulse)*signal*uGlow*uFade,1.);}`}));thread.frustumCulled=false;scene.add(thread);
 const halo=new THREE.Points(haloGeometry,new THREE.ShaderMaterial({uniforms,transparent:true,depthWrite:false,blending:THREE.AdditiveBlending,
 vertexShader:`${harvestUniforms}uniform float uPixelRatio;${waveCode}${harvestCode}void main(){gl_Position=projectionMatrix*viewMatrix*vec4(harvestCentre(),1.);gl_PointSize=42.*uPixelRatio*uRadiusScale;}`,
 fragmentShader:`uniform float uProgress,uOnset,uFade,uGlow;void main(){float signal=smoothstep(uOnset-.03,uOnset+.08,uProgress);float d=length(gl_PointCoord-.5)*2.;gl_FragColor=vec4(vec3(.055,.035,.012)*exp(-d*d*5.)*signal*uGlow*uFade,1.);}`}));halo.frustumCulled=false;scene.add(halo);
 if(source.z>-2){
  const tag=new THREE.Mesh(new THREE.PlaneGeometry(1.7,.51,20,8),new THREE.ShaderMaterial({uniforms,transparent:true,depthWrite:false,side:THREE.DoubleSide,
  vertexShader:`${harvestUniforms}varying vec2 vUv;${waveCode}${harvestCode}void main(){vUv=uv;vec2 base=harvestBase()+vec2(position.x,-position.y*1.35-.60);vec3 p=pointAt(base);p.y+=.05;gl_Position=projectionMatrix*viewMatrix*vec4(p,1.);}`,
  fragmentShader:`uniform sampler2D uCaption;uniform float uProgress,uOnset,uFade;varying vec2 vUv;void main(){float signal=smoothstep(uOnset+.15,uOnset+.23,uProgress);vec4 c=texture2D(uCaption,vUv);gl_FragColor=vec4(c.rgb,c.a*signal*uFade*.88);}`}));tag.frustumCulled=false;scene.add(tag);
 }
 harvestInstances.push({source,sourceId,uniforms});
}
// Sparse warm clues remain on the sea, alongside the four gold flow paths.
let seed=47;function random(){seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;}
const pointPositions=[],pointSeeds=[],pointSizes=[];
for(let i=0;i<520;i++){pointPositions.push((random()-.5)*48,0,-28+random()*39);pointSeeds.push(random());pointSizes.push(.6+Math.pow(random(),9)*1.6);}
const pointGeometry=new THREE.BufferGeometry();pointGeometry.setAttribute('position',new THREE.Float32BufferAttribute(pointPositions,3));pointGeometry.setAttribute('aSeed',new THREE.Float32BufferAttribute(pointSeeds,1));pointGeometry.setAttribute('aSize',new THREE.Float32BufferAttribute(pointSizes,1));
const points=new THREE.Points(pointGeometry,new THREE.ShaderMaterial({uniforms:{...shared,uPixelRatio:{value:renderer.getPixelRatio()}},transparent:true,depthWrite:false,blending:THREE.AdditiveBlending,
vertexShader:`uniform float uTime,uWave,uPixelRatio;attribute float aSeed,aSize;varying float vSeed,vFade;${waveCode}
void main(){vSeed=aSeed;vec2 p=position.xz;p.x=mod(p.x+uCurrentTravel.x+24.,48.)-24.;p.y+=uCurrentTravel.y;vec3 w=pointAt(p);w.y+=.035;vec4 mv=modelViewMatrix*vec4(w,1.);vFade=exp(-max(-mv.z-10.,0.)*.045);gl_Position=projectionMatrix*mv;gl_PointSize=clamp(aSize*25./(-mv.z),1.1,4.)*uPixelRatio;}`,
fragmentShader:`uniform float uTime,uGlow;varying float vSeed,vFade;void main(){float d=length(gl_PointCoord-.5)*2.;float glow=.25+.75*pow(.5+.5*sin(uTime*.4+vSeed*39.),3.);gl_FragColor=vec4(vec3(1.4,1.0,.46)*exp(-d*d*5.)*(1.-smoothstep(.65,1.,d))*glow*vFade*uGlow*.85,1.);}`}));points.frustumCulled=false;scene.add(points);
// HDR scene, separable bloom and depth-based soft focus. Tone map only once.
const target=new THREE.WebGLRenderTarget(1,1,{type:THREE.HalfFloatType,depthBuffer:true,samples:0});
const bloomA=new THREE.WebGLRenderTarget(1,1,{type:THREE.HalfFloatType,depthBuffer:false}),bloomB=bloomA.clone();


const postScene=new THREE.Scene(),postCamera=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
const postVertex=`varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}`;
const blur=new THREE.ShaderMaterial({depthTest:false,depthWrite:false,uniforms:{uTexture:{value:target.texture},uStep:{value:new THREE.Vector2()},uThreshold:{value:1}},vertexShader:postVertex,fragmentShader:`varying vec2 vUv;uniform sampler2D uTexture;uniform vec2 uStep;uniform float uThreshold;
vec3 sampleColor(vec2 uv){vec3 c=texture2D(uTexture,uv).rgb;
 float luminance=dot(c,vec3(.2126,.7152,.0722));
 // Bounded optical glare: narrow hot glints cannot bleed giant orange blobs
 // across the water or foreground tax records.
 vec3 glare=c*(max(0.,luminance-1.5)/max(luminance,.0001))*min(1.,6./max(luminance,.0001));
 return mix(c,glare,uThreshold);}
void main(){vec3 c=sampleColor(vUv)*.227027;c+=sampleColor(vUv+uStep*1.384615)*.316216;c+=sampleColor(vUv-uStep*1.384615)*.316216;c+=sampleColor(vUv+uStep*3.230769)*.070270;c+=sampleColor(vUv-uStep*3.230769)*.070270;gl_FragColor=vec4(c,1.);}`});
  /* ACES fitted approximation by Stephen Hill, adapted from BakingLab/ACES.hlsl
   * https://github.com/TheRealMJP/BakingLab/blob/master/BakingLab/ACES.hlsl
   * MIT License - Copyright (c) 2016 MJP
   * Permission is hereby granted, free of charge, to any person obtaining a copy
   * of this software and associated documentation files (the "Software"), to deal
   * in the Software without restriction, including without limitation the rights
   * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   * copies of the Software, and to permit persons to whom the Software is
   * furnished to do so, subject to the following conditions:
   * The above copyright notice and this permission notice shall be included in all
   * copies or substantial portions of the Software.
   * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
   * SOFTWARE.
   */
  const toneCode=`vec3 cameraTone(vec3 color){
    const mat3 inputMatrix=mat3(.59719,.07600,.02840,.35458,.90834,.13383,.04823,.01566,.83777);
    const mat3 outputMatrix=mat3(1.60475,-.10208,-.00327,-.53108,1.10813,-.07276,-.07367,-.00605,1.07602);
    vec3 v=inputMatrix*max(color,vec3(0));
    vec3 a=v*(v+.0245786)-.000090537,b=v*(.983729*v+.432951)+.238081;
    return clamp(outputMatrix*(a/b),0.,1.);
  }`;

const composite=new THREE.ShaderMaterial({depthTest:false,depthWrite:false,uniforms:{uTexture:{value:target.texture},uBloom:{value:bloomA.texture},uGlow:shared.uGlow,uResolution:shared.uResolution,uGlassRect:{value:new THREE.Vector4()},uGlassRadius:{value:22},uGlassPixelRatio:{value:1}},vertexShader:postVertex,fragmentShader:`
varying vec2 vUv;uniform sampler2D uTexture,uBloom;uniform float uGlow,uGlassRadius,uGlassPixelRatio;uniform vec2 uResolution;uniform vec4 uGlassRect;
${toneCode}
vec3 sceneRadiance(vec2 uv){uv=clamp(uv,vec2(.001),vec2(.999));return texture2D(uTexture,uv).rgb+texture2D(uBloom,uv).rgb*.003*uGlow;}
void main(){vec3 color=sceneRadiance(vUv);
 // Screen-space convex lens: refract the same live scene texture, before tone
 // mapping, without a second renderer or distorting any HTML text.
 if(uGlassRect.z>0.){
  vec2 halfSize=uGlassRect.zw*uResolution*.5;
  vec2 local=(vUv-uGlassRect.xy-uGlassRect.zw*.5)*uResolution;
  float radius=min(uGlassRadius*uGlassPixelRatio,min(halfSize.x,halfSize.y));
  vec2 q=abs(local)-halfSize+radius;
  float sd=length(max(q,vec2(0.)))+min(max(q.x,q.y),0.)-radius;
  float inside=1.-smoothstep(-.7,.7,sd);
  if(inside>0.){
   vec2 normal=sign(local)*(length(max(q,vec2(0.)))>.001?normalize(max(q,vec2(0.))):q.x>q.y?vec2(1.,0.):vec2(0.,1.));
   float depth=max(-sd,0.),edge=exp(-depth/(13.*uGlassPixelRatio));
   vec2 bend=-normal*(9.*edge)*uGlassPixelRatio-local/max(halfSize,vec2(1.))*(1.-edge)*1.4*uGlassPixelRatio;
   vec2 uv=vUv+bend/uResolution,blurStep=vec2(1.15*uGlassPixelRatio)/uResolution;
   vec3 transmitted=sceneRadiance(uv)*.52;
   transmitted+=(sceneRadiance(uv+vec2(blurStep.x,0.))+sceneRadiance(uv-vec2(blurStep.x,0.))+sceneRadiance(uv+vec2(0.,blurStep.y))+sceneRadiance(uv-vec2(0.,blurStep.y)))*.12;
   vec3 reflected=sceneRadiance(vec2(clamp(vUv.x+normal.x*.02,.01,.99),mix(vUv.y,.88,.65)));
   float rim=exp(-depth/(1.7*uGlassPixelRatio));
   float lightFacing=pow(max(dot(normal,normalize(vec2(-.65,.75))),0.),2.);
   vec3 glass=mix(transmitted,reflected,.055+edge*.16)+reflected*rim*lightFacing*.12;
   color=mix(color,glass,inside);
  }
 }

color*=1.-.06*pow(length((vUv-.5)*vec2(1.1,.8)),1.4);gl_FragColor=vec4(cameraTone(color*6.),1.);
#include <colorspace_fragment>
}`});
const quad=new THREE.Mesh(new THREE.PlaneGeometry(2,2),composite);postScene.add(quad);
function resize(){renderer.setPixelRatio(Math.min(devicePixelRatio,1.25,Math.sqrt(1500000/(innerWidth*innerHeight))));renderer.setSize(innerWidth,innerHeight,false);camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();updateOceanGrid();shared.uSunDirection.value.set(.54,-.04,1.).unproject(camera).sub(camera.position).normalize();shared.uClearDirection.value.copy(shared.uSunDirection.value);fitRecords();const size=renderer.getDrawingBufferSize(new THREE.Vector2());target.setSize(size.x,size.y);shared.uResolution.value.copy(size);composite.uniforms.uGlassPixelRatio.value=size.y/innerHeight;bloomA.setSize(Math.ceil(size.x/4),Math.ceil(size.y/4));bloomB.setSize(Math.ceil(size.x/4),Math.ceil(size.y/4));points.material.uniforms.uPixelRatio.value=renderer.getPixelRatio();for(const item of harvestInstances)item.uniforms.uPixelRatio.value=renderer.getPixelRatio();if(state.ready&&!state.lost&&state.active)draw();}
addEventListener('resize',resize);resize();
function draw(){updateMaterialFlow();updateWaveCache();updateSheetPose();updateSkyCache();renderer.setClearColor(0x050f25,1);renderer.setRenderTarget(target);renderer.render(scene,camera);
quad.material=blur;blur.uniforms.uTexture.value=target.texture;blur.uniforms.uThreshold.value=1;blur.uniforms.uStep.value.set(2/target.width,0);renderer.setRenderTarget(bloomA);renderer.render(postScene,postCamera);
blur.uniforms.uTexture.value=bloomA.texture;blur.uniforms.uThreshold.value=0;blur.uniforms.uStep.value.set(0,1/bloomA.height);renderer.setRenderTarget(bloomB);renderer.render(postScene,postCamera);
blur.uniforms.uTexture.value=bloomB.texture;blur.uniforms.uStep.value.set(1/bloomB.width,0);renderer.setRenderTarget(bloomA);renderer.render(postScene,postCamera);


quad.material=composite;renderer.setRenderTarget(null);renderer.render(postScene,postCamera);state.frames++;state.ready=true;}
let previous=performance.now(),fpsTime=previous,fpsFrames=0,frameId=0;
function animate(now){
 frameId=0;if(!state.active||document.hidden||state.lost||state.paused)return;
 if(now-previous<1000/60-.5){frameId=requestAnimationFrame(animate);return;}
 const delta=Math.max(0,Math.min((now-previous)/1000,.05));previous=now;
 state.time+=delta*state.speed;shared.uTime.value=state.time;draw();fpsFrames++;
 if(now-fpsTime>1000){state.fps=Math.round(fpsFrames*1000/(now-fpsTime));fpsTime=now;fpsFrames=0;}
 frameId=requestAnimationFrame(animate);
}
function resume(){if(state.active&&!document.hidden&&!state.lost&&!state.paused&&!frameId){previous=performance.now();frameId=requestAnimationFrame(animate);}}
function setPaused(value){state.paused=!!value;if(state.paused){cancelAnimationFrame(frameId);frameId=0;}else resume();}
function setActive(value){state.active=!!value;if(!state.active){cancelAnimationFrame(frameId);frameId=0;}else resume();}
reduced.addEventListener('change',event=>setPaused(event.matches));
canvas.addEventListener('webglcontextlost',event=>{event.preventDefault();state.lost=true;cancelAnimationFrame(frameId);frameId=0;canvas.dispatchEvent(new Event('ocean-unavailable'));});
canvas.addEventListener('webglcontextrestored',()=>{state.lost=false;waveCacheTime=NaN;skyCacheTime=NaN;draw();canvas.dispatchEvent(new Event('ocean-restored'));resume();});
draw();resume();
function readSunTransmission(){const d=shared.uSunDirection.value,pixel=new Uint16Array(4);
 const x=((Math.atan2(d.x,d.z)/(2*Math.PI)+.5)%1+1)%1;
 const y=Math.sqrt(Math.asin(Math.max(0,Math.min(1,d.y)))/(Math.PI*.5));
 renderer.readRenderTargetPixels(skyFront,Math.min(skyWidth-1,Math.floor(x*skyWidth)),Math.min(skyHeight-1,Math.floor(y*skyHeight)),1,1,pixel);
 return THREE.DataUtils.fromHalfFloat(pixel[3]);}
const api={setActive,setGlassPanel(rect,radius=22){const u=composite.uniforms;if(rect&&rect.width>0&&rect.height>0){u.uGlassRect.value.set(rect.left/innerWidth,(innerHeight-rect.bottom)/innerHeight,rect.width/innerWidth,rect.height/innerHeight);u.uGlassRadius.value=radius;}else u.uGlassRect.value.set(0,0,0,0);if(state.paused&&!state.lost)draw();},surfaceProbe(){const p=new Uint16Array(24),wave=new Uint16Array(4);renderer.readRenderTargetPixels(sheetPoseTarget,0,0,6,1,p);renderer.readRenderTargetPixels(waveTargets[0].target,256,256,1,1,wave);const values=Array.from(p,THREE.DataUtils.fromHalfFloat);return {waveOrigin:Array.from(wave,THREE.DataUtils.fromHalfFloat),support:Array.from({length:6},(_,i)=>values.slice(i*4,i*4+3)),currentTravel:shared.uCurrentTravel.value.toArray()};},radianceProbe(x,y){const p=new Uint16Array(4);
 renderer.readRenderTargetPixels(target,Math.max(0,Math.min(target.width-1,Math.floor(x*target.width))),Math.max(0,Math.min(target.height-1,Math.floor((1-y)*target.height))),1,1,p);
 return Array.from(p,THREE.DataUtils.fromHalfFloat);},pause:()=>setPaused(true),play:()=>setPaused(false),seek(time){state.time=Math.max(0,Number(time)||0);shared.uTime.value=state.time;skyCacheTime=NaN;draw();},setSunDirection(x,y,z){shared.uSunDirection.value.set(x,y,z).normalize();skyCacheTime=NaN;draw();},diagnostics:()=>({...state,width:innerWidth,height:innerHeight,webglError:renderer.getContext().getError(),waterVertices:waterGeometry.attributes.position.count,projectedGrid:[gridX,gridY],continuousFootprint:true,goldFlowPaths:4,dataMarkers:markerDefinitions.length,pearl:true,pearlCount:harvestInstances.length,harvestMode:'per-material',surfaceAttachment:'shared-displaced-water',pageSurfaceOffset:.015,sheetMaxSeparation:.035,sheetContactMode:"flexible-surface-with-continuous-wetting",waterFlattening:false,pearlCentreSurfaceOffset:.07,animatedHarvestLift:false,captionAttachment:'water-surface',recordInkReadabilityFloor:.12,pearls:harvestInstances.map(h=>({sourceId:h.sourceId,x:h.uniforms.uAnchor.value.x+h.uniforms.uHeight.value*.94+.12,progress:h.uniforms.uProgress.value,fade:h.uniforms.uFade.value,cycle:h.source.cycle})),labels:sourceTypes,taxRecords:recordDefinitions.length,dataStreams:7,materialDirection:'left-to-right',materialSpacing:"shared-speed-separated-lanes",records:recordMeshes.map((r,id)=>({id,kind:r.currentKind,lane:r.lane,z:r.z,anchorZ:r.mesh.material.uniforms.uAnchor.value.y,yaw:r.mesh.material.uniforms.uYaw.value,width:r.width*r.scale,x:r.flow.x,cycle:r.cycle,opacity:r.mesh.material.uniforms.uOpacity.value,bound:r.flow.bound})),lighting:"shared-photometric-sun-atmosphere",sunVisible:shared.uSunDirection.value.y>0&&readSunTransmission()>.02,sunTransmission:readSunTransmission(),sunScreen:[.77,.52],sunDirection:shared.uSunDirection.value.toArray(),sky:"procedural-volume-sky",sharedSkyReflections:true,skyModel:"reference-cloud-volume-shared-rgb-atmosphere",sunOcclusion:"integrated-volume-transmittance",cloudLightSamples:3,cloudViewSamples:64,cloudWind:28,cloudCacheSeconds:.7,waveProfile:"8-long-swells-16-crossed-middle-waves-16-wind-waves-8-short-waves",waveAdvection:"shared-current-and-dispersive-orbits",currentVelocityRange:[.28,.40],sheetSupportSamples:5,waveCachePasses:2,waveCacheMRT:true,supportCachePasses:0,supportCacheBaked:false,waveComponents:48,windRippleComponents:8,solarDiscSamples:5,solarIlluminanceLux:solarModel.normalIlluminanceLux,solarAngularRadius:solarModel.angularRadius,solarSolidAngle,preExposure:solarModel.preExposure,cameraExposure:6,skyIrradiance:shared.uSkyIrradiance.value.toArray(),solarTransmission:atmosphereTransmission(shared.uSunDirection.value.y),solarRadiance:atmosphereTransmission(shared.uSunDirection.value.y).map(t=>t*solarModel.normalIlluminanceLux*solarModel.preExposure*state.glow/solarSolidAngle*(shared.uSunDirection.value.y>0?1:0)),solarIrradiance:atmosphereTransmission(shared.uSunDirection.value.y).map(t=>t*solarModel.normalIlluminanceLux*solarModel.preExposure*state.glow*(shared.uSunDirection.value.y>0?1:0)),atmosphereViewSamples:12,specularVarianceFiltering:true,cloudSunlightFloor:0,pearlSkyLighting:true,pearlMaterial:"ivory-nacre-subsurface",boundedBloom:true,artificialDepthBlur:false,filteredSkyReflections:true,portedReferenceFunctions:["solarColor","waterPhase","cloudSolar","cloudPhase","density","cirrus","sky-volume-integration","sun-disc-and-aureole","fresnel","solarSpecular"],reflectionOcclusion:"neighbor-wave-horizon",cloudWeatherCoverage:"reference-weather",sunClearSector:false,dataContrast:"opaque-navy-ivory",skyResolution:[skyWidth,skyHeight],skyUpdateRows:skyStrip,skyScissorSpace:"render-target-pixels",usesSkyBitmap:false,waveCacheResolution:[[512,512],[256,256]],scenePixelBudget:1500000,recordTextureResolution:[2048,1280],recordVisibleRows:2,recordFontVerticalCompensation:true,nearRigidFloatingSheets:0,nearFlexibleFloatingSheets:6,nearSheetSlopeLimit:.08,renderPixelRatio:renderer.getPixelRatio(),worldSpaceScattering:false,waveShadow:true,particles:pointSeeds.length,glassRefraction:'single-pass-screen-space-convex-lens',glassExtraSceneSamples:6,glassExtraBloomSamples:6,renderType:'directional-ocean',ocean:true,backgroundOnly:true,usesOceanBitmap:false})};

return api;
}
