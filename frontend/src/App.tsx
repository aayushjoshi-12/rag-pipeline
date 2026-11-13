import { NavLink, Route, Routes } from 'react-router-dom'

function Placeholder({ title }: { title: string }) {
  return <h1>{title}</h1>
}

export default function App() {
  return (
    <div>
      <nav>
        <NavLink to="/">Overview</NavLink> <NavLink to="/documents">Documents</NavLink>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<Placeholder title="Overview" />} />
          <Route path="/documents" element={<Placeholder title="Documents" />} />
        </Routes>
      </main>
    </div>
  )
}
