import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/chakra-petch/latin-400.css'
import '@fontsource/chakra-petch/latin-500.css'
import '@fontsource/chakra-petch/latin-600.css'
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import './styles/orbit.css'
import './styles/app.css'
import './styles/stage.css'
import App from './App.tsx'
import { RemindersProvider } from './reminders/RemindersProvider.tsx'
import { StageProvider } from './stage/StageProvider.tsx'
import { VoiceProvider } from './voice/VoiceProvider.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <VoiceProvider>
      <RemindersProvider>
        <StageProvider>
          <App />
        </StageProvider>
      </RemindersProvider>
    </VoiceProvider>
  </StrictMode>,
)
