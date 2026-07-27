class ApplicationService
  def self.call(**kwargs)
    new(**kwargs).call
  end

  def call
    raise NotImplementedError, "#{self.class.name} must implement #call"
  end
end
