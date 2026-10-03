import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { applyTheme } from './theme';
import './styles.css';
import './enterprise.css';
import './bi.css';
import './chat.css';

applyTheme(document.documentElement);
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
