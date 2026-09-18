import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// No StrictMode: its double-invoke of effects trips Recharts 3's ResizeObserver
// sizing (charts render at 0px and never recover).
createRoot(document.getElementById('root')).render(<App />)
