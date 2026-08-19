import './app.css';
import App from './App.svelte';

const target = document.getElementById('app');

if (!target) {
  throw new Error('App mount point not found');
}

// Alignment validation now lives inside the Data workspace (Timing facet), not a
// separate page -- one home for the whole measure/beat ground-truth job.
const app = new App({
  target,
});

export default app;
