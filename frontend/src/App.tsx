import { NavLink, Route, Routes } from 'react-router-dom'
import ChatPage from './pages/ChatPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import DocumentsPage from './pages/DocumentsPage'
import OverviewPage from './pages/OverviewPage'

export default function App() {
  return (
    <div className="shell">
      <nav>
        <strong>rag-pipeline</strong>
        <NavLink to="/" end>
          Overview
        </NavLink>
        <NavLink to="/documents">Documents</NavLink>
        <NavLink to="/chat">Chat</NavLink>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/documents/:id" element={<DocumentDetailPage />} />
          <Route path="/chat" element={<ChatPage />} />
        </Routes>
      </main>
    </div>
  )
}
