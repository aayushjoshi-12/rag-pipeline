import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from './App'

describe('App shell', () => {
  it('renders the overview route', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument()
  })

  it('renders the documents route', () => {
    render(
      <MemoryRouter initialEntries={['/documents']}>
        <App />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'Documents' })).toBeInTheDocument()
  })
})
