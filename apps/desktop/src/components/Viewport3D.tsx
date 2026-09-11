/**
 * 3D 检查视口（three.js + r3f + drei）。
 *
 * 职责边界（见 docs/ARCHITECTURE.md）：**只做检查与摆放，不做顶点级网格编辑**。
 * 那是 DCC 的活。超出能力范围的操作应该引导用户回 Blender/Maya，而不是在这里硬做。
 *
 * 环境光用三套灯组模拟，不用 drei 的 HDRI 预设 —— 后者要从 CDN 拉贴图，
 * 离线或断网时会静默失败，而"打光可预期"对判断材质更重要。
 */

import { Suspense, useEffect, useMemo, useState } from 'react';
import { Canvas } from '@react-three/fiber';
import { Grid, OrbitControls, useGLTF } from '@react-three/drei';
import * as THREE from 'three';

export type DisplayMode = 'clay' | 'wireframe' | 'material';
export type LightRig = 'studio' | 'outdoor' | 'neutral';

interface ModelProps {
  url: string;
  mode: DisplayMode;
  highlight?: number[];
  onStats?: (stats: { size: [number, number, number]; triangles: number }) => void;
}

function findFirstMesh(root: THREE.Object3D): THREE.Mesh | null {
  let found: THREE.Mesh | null = null;
  root.traverse((child) => {
    if (!found && (child as THREE.Mesh).isMesh) found = child as THREE.Mesh;
  });
  return found;
}

/** 把定位器给的面索引抽成一个独立的高亮几何体。
 *
 *  做法是复制这些三角形而不是给原几何体上色 —— 这样切显示模式、切版本都不用重建材质，
 *  也不会污染从 GLTFLoader 缓存里拿到的共享几何体。
 */
function useHighlightGeometry(base: THREE.BufferGeometry | null, indices?: number[]) {
  return useMemo(() => {
    if (!base || !indices || indices.length === 0) return null;

    const position = base.getAttribute('position');
    if (!position) return null;
    const index = base.getIndex();
    const vertexCount = index ? index.count : position.count;
    const triangles = Math.floor(vertexCount / 3);

    const picked: number[] = [];
    for (const faceIndex of indices) {
      if (faceIndex < 0 || faceIndex >= triangles) continue;
      for (let corner = 0; corner < 3; corner += 1) {
        const positionIndex = index ? index.getX(faceIndex * 3 + corner) : faceIndex * 3 + corner;
        picked.push(positionIndex);
      }
    }
    if (picked.length === 0) return null;

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', position.clone());
    geometry.setIndex(picked);
    geometry.computeVertexNormals();
    return geometry;
  }, [base, indices]);
}

function Model({ url, mode, highlight, onStats }: ModelProps) {
  const gltf = useGLTF(url);
  const scene = useMemo(() => gltf.scene.clone(true), [gltf.scene]);
  const [mesh, setMesh] = useState<THREE.Mesh | null>(null);

  useEffect(() => {
    const first = findFirstMesh(scene);
    setMesh(first);

    const box = new THREE.Box3().setFromObject(scene);
    const size = new THREE.Vector3();
    box.getSize(size);

    let triangles = 0;
    scene.traverse((child) => {
      const candidate = child as THREE.Mesh;
      if (!candidate.isMesh) return;
      const geometry = candidate.geometry as THREE.BufferGeometry;
      const index = geometry.getIndex();
      triangles += (index ? index.count : geometry.getAttribute('position').count) / 3;
    });

    onStats?.({ size: [size.x, size.y, size.z], triangles: Math.round(triangles) });
  }, [scene, onStats]);

  // 显示模式：clay / 线框 / 原材质
  useEffect(() => {
    scene.traverse((child) => {
      const candidate = child as THREE.Mesh;
      if (!candidate.isMesh) return;

      if (mode === 'clay') {
        candidate.material = new THREE.MeshStandardMaterial({
          color: 0xb8b5ad,
          roughness: 0.62,
          metalness: 0.0,
        });
      } else if (mode === 'wireframe') {
        candidate.material = new THREE.MeshBasicMaterial({ color: 0x5f5e5a, wireframe: true });
      } else if (Array.isArray(candidate.material)) {
        candidate.material.forEach((m) => {
          (m as THREE.MeshStandardMaterial).wireframe = false;
        });
      }
    });
  }, [scene, mode]);

  const highlightGeometry = useHighlightGeometry(mesh?.geometry ?? null, highlight);

  return (
    <group>
      <primitive object={scene} />
      {highlightGeometry && (
        <mesh geometry={highlightGeometry}>
          <meshBasicMaterial
            color={0xd4537e}
            transparent
            opacity={0.55}
            side={THREE.DoubleSide}
            depthWrite={false}
            polygonOffset
            polygonOffsetFactor={-2}
          />
        </mesh>
      )}
    </group>
  );
}

function Lights({ rig }: { rig: LightRig }) {
  if (rig === 'outdoor') {
    return (
      <>
        <ambientLight intensity={0.55} />
        <directionalLight position={[6, 10, 4]} intensity={2.4} color={0xfff6e0} />
        <directionalLight position={[-6, 4, -4]} intensity={0.6} color={0xdce8ff} />
      </>
    );
  }
  if (rig === 'neutral') {
    return (
      <>
        <ambientLight intensity={0.9} />
        <directionalLight position={[3, 5, 3]} intensity={1.1} />
      </>
    );
  }
  return (
    <>
      <ambientLight intensity={0.42} />
      <directionalLight position={[4, 6, 5]} intensity={2.0} />
      <directionalLight position={[-5, 3, -4]} intensity={0.75} color={0xe8eefc} />
      <directionalLight position={[0, -4, 2]} intensity={0.3} />
    </>
  );
}

interface Viewport3DProps {
  url: string | null;
  highlight?: number[];
  emptyHint?: string;
}

export function Viewport3D({ url, highlight, emptyHint = '选择一个版本后在此检查' }: Viewport3DProps) {
  const [mode, setMode] = useState<DisplayMode>('material');
  const [rig, setRig] = useState<LightRig>('studio');
  const [showGrid, setShowGrid] = useState(true);
  const [stats, setStats] = useState<{ size: [number, number, number]; triangles: number } | null>(null);

  if (!url) {
    return <div className="viewport" style={{ display: 'grid', placeItems: 'center' }}>
      <span className="muted">{emptyHint}</span>
    </div>;
  }

  const longest = stats ? Math.max(...stats.size) : 0;

  return (
    <div className="viewport">
      <div className="overlay">
        {(['material', 'clay', 'wireframe'] as DisplayMode[]).map((value) => (
          <button key={value} className={mode === value ? 'active' : ''} onClick={() => setMode(value)}>
            {value === 'material' ? '材质' : value === 'clay' ? 'Clay' : '线框'}
          </button>
        ))}
        <span style={{ width: 12 }} />
        {(['studio', 'outdoor', 'neutral'] as LightRig[]).map((value) => (
          <button key={value} className={rig === value ? 'active' : ''} onClick={() => setRig(value)}>
            {value === 'studio' ? '棚拍' : value === 'outdoor' ? '户外' : '中性'}
          </button>
        ))}
        <span style={{ width: 12 }} />
        <button className={showGrid ? 'active' : ''} onClick={() => setShowGrid((v) => !v)}>
          网格
        </button>
      </div>

      {stats && (
        <div
          style={{
            position: 'absolute',
            bottom: 10,
            left: 10,
            fontSize: 12,
            color: '#5f5e5a',
            background: 'rgba(255,255,255,0.86)',
            padding: '4px 8px',
            borderRadius: 6,
          }}
        >
          三角面 {stats.triangles.toLocaleString()} · 尺寸{' '}
          {stats.size.map((v) => v.toFixed(3)).join(' × ')} m · 最长边 {longest.toFixed(3)} m
        </div>
      )}

      <Canvas
        camera={{ position: [1.9, 1.5, 1.9], fov: 42, near: 0.01, far: 200 }}
        dpr={[1, 2]}
        style={{ height: 420 }}
      >
        <color attach="background" args={['#e9e7e0']} />
        <Suspense fallback={null}>
          <Lights rig={rig} />
          <Model url={url} mode={mode} highlight={highlight} onStats={setStats} />
          {showGrid && (
            <Grid
              args={[20, 20]}
              cellSize={0.25}
              cellColor="#c9c6bd"
              sectionSize={1}
              sectionColor="#a8a49a"
              infiniteGrid
              fadeDistance={14}
              position={[0, -0.001, 0]}
            />
          )}
          <OrbitControls makeDefault enableDamping dampingFactor={0.12} />
        </Suspense>
      </Canvas>
    </div>
  );
}

export default Viewport3D;
