// Generates a fake "live view" frame as a data URL so the UI has something to render
// before real camera streaming exists.
export function generateMockFrame(seed: number): string {
  const width = 480;
  const height = 320;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;

  ctx.fillStyle = "#05070d";
  ctx.fillRect(0, 0, width, height);

  let rand = seed;
  const next = () => {
    rand = (rand * 9301 + 49297) % 233280;
    return rand / 233280;
  };

  for (let i = 0; i < 120; i++) {
    const x = next() * width;
    const y = next() * height;
    const r = next() * 1.5;
    const brightness = 150 + next() * 105;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = `rgb(${brightness}, ${brightness}, ${brightness})`;
    ctx.fill();
  }

  return canvas.toDataURL("image/png");
}
