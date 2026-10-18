import { createRoot } from 'react-dom/client';
import { App } from './App';
import { SessionController } from '@/business/adapter';
import './styles.css';

const controller = new SessionController();
createRoot(document.getElementById('root')!).render(<App controller={controller}/>);
